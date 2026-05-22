"""
Connects to Binance WebSocket streams, computes directional score every 1s,
and writes to data/1s_scores.jsonl.
"""
import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

import aiohttp

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import config

logger = logging.getLogger(__name__)

WS_URL = (
    f"{config.BINANCE_WS}/stream?streams="
    "btcusdt@aggTrade/btcusdt@bookTicker/btcusdt@diffDepth"
)

LARGE_LOT_THRESHOLD = 1.0  # BTC

class Observer:
    def __init__(self):
        self._agg_trades: deque = deque(maxlen=500)
        self._book_ticker: dict = {}
        self._depth_updates: deque = deque(maxlen=200)
        self._last_trade_price: float | None = None
        self._last_score_ts: int = 0
        self._running = False
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _handle_agg_trade(self, msg: dict) -> None:
        price = float(msg["p"])
        self._last_trade_price = price
        self._agg_trades.append({
            "price": price,
            "qty": float(msg["q"]),
            "is_buyer_maker": msg["m"],
            "ts": msg["T"],
        })

    def _handle_book_ticker(self, msg: dict) -> None:
        self._book_ticker = {
            "bid": float(msg["b"]),
            "ask": float(msg["a"]),
            "bid_qty": float(msg["B"]),
            "ask_qty": float(msg["A"]),
        }

    def _resolve_price(self, recent_trades: list[dict]) -> float | None:
        bid = self._book_ticker.get("bid")
        ask = self._book_ticker.get("ask")
        if bid is not None and ask is not None:
            return (float(bid) + float(ask)) / 2.0
        if recent_trades:
            return float(recent_trades[-1]["price"])
        if self._last_trade_price is not None:
            return float(self._last_trade_price)
        return None

    def _handle_depth(self, msg: dict) -> None:
        self._depth_updates.append({
            "bids": msg.get("b", []),
            "asks": msg.get("a", []),
            "ts": self._now_ms(),
        })

    def _compute_score(self) -> dict:
        now_ms = self._now_ms()
        window_ms = config.SCORE_INTERVAL_MS

        recent_trades = [t for t in self._agg_trades if now_ms - t["ts"] <= window_ms]
        recent_depth = [d for d in self._depth_updates if now_ms - d["ts"] <= window_ms * 2]

        data_quality = 1.0 if recent_trades else 0.0

        # Delta: buy qty - sell qty
        buy_vol = sum(t["qty"] for t in recent_trades if not t["is_buyer_maker"])
        sell_vol = sum(t["qty"] for t in recent_trades if t["is_buyer_maker"])
        total_vol = buy_vol + sell_vol or 1e-9
        delta = buy_vol - sell_vol
        delta_norm = max(-1.0, min(1.0, delta / (total_vol * 0.5 + 1e-9)))

        # Order book imbalance
        bid_qty = self._book_ticker.get("bid_qty", 0.0)
        ask_qty = self._book_ticker.get("ask_qty", 0.0)
        total_bq = bid_qty + ask_qty or 1e-9
        imbalance = (bid_qty - ask_qty) / total_bq

        # Depth: bid added / ask removed (bullish pressure)
        bid_added = 0.0
        ask_removed = 0.0
        for d in recent_depth:
            for price, qty in d["bids"]:
                q = float(qty)
                if q > 0:
                    bid_added += q
            for price, qty in d["asks"]:
                q = float(qty)
                if q == 0:
                    ask_removed += 1.0

        bid_added = min(1.0, bid_added / 10.0)
        ask_removed = min(1.0, ask_removed / 10.0)

        # Price displacement
        prices = [t["price"] for t in recent_trades]
        price_displacement_up = 0.0
        if len(prices) >= 2:
            move = (prices[-1] - prices[0]) / (prices[0] + 1e-9)
            price_displacement_up = max(0.0, min(1.0, move * 1000))

        # Large lot detection
        large_lot_buy = 1.0 if any(
            t["qty"] >= LARGE_LOT_THRESHOLD and not t["is_buyer_maker"]
            for t in recent_trades
        ) else 0.0
        large_lot_sell = 1.0 if any(
            t["qty"] >= LARGE_LOT_THRESHOLD and t["is_buyer_maker"]
            for t in recent_trades
        ) else 0.0

        # Absorption penalty: high volume but no price movement
        absorption = 0.0
        if total_vol > 0.5 and len(prices) >= 2:
            price_range = abs(prices[-1] - prices[0]) / (prices[0] + 1e-9)
            if price_range < 0.0002:
                absorption = min(1.0, total_vol / 2.0)

        # One-sided: each component only contributes to its own direction
        LONG_SCORE = (
            max(0.0, delta_norm) * 3.0
            + max(0.0, imbalance) * 2.0
            + ask_removed * 1.0
            + bid_added * 1.0
            + price_displacement_up * 2.0
            + large_lot_buy * 1.0
        )

        price_displacement_down = max(0.0, -min(0.0, (prices[-1] - prices[0]) / (prices[0] + 1e-9) * 1000)) if len(prices) >= 2 else 0.0
        SHORT_SCORE = (
            max(0.0, -delta_norm) * 3.0
            + max(0.0, -imbalance) * 2.0
            + price_displacement_down * 2.0
            + large_lot_sell * 1.0
        )

        # Absorption reduces the dominant side
        net = LONG_SCORE - SHORT_SCORE
        absorption_adj = absorption * 1.0 * (1.0 if net >= 0 else -1.0)
        raw = net - absorption_adj
        max_possible = 10.0
        score = max(-10.0, min(10.0, raw / max_possible * 10.0))

        dominant = "LONG" if score > 0 else ("SHORT" if score < 0 else "NEUTRAL")
        price = self._resolve_price(recent_trades)

        return {
            "timestamp_ms": now_ms,
            "score": round(score, 4),
            "dominant": dominant,
            "price": round(price, 2) if price is not None else None,
            "delta": round(delta, 6),
            "imbalance": round(imbalance, 4),
            "absorption": round(absorption, 4),
            "large_lot": large_lot_buy > 0 or large_lot_sell > 0,
            "data_quality": round(data_quality, 2),
        }

    async def _write_score(self) -> None:
        record = self._compute_score()
        line = json.dumps(record) + "\n"
        with open(config.SCORES_FILE, "a", encoding="utf-8") as f:
            f.write(line)

    async def _score_loop(self) -> None:
        while self._running:
            await asyncio.sleep(config.SCORE_INTERVAL_MS / 1000.0)
            try:
                await self._write_score()
            except Exception as e:
                logger.warning(f"Score write error: {e}")

    async def _dispatch(self, msg: dict) -> None:
        stream = msg.get("stream", "")
        data = msg.get("data", msg)
        try:
            if "aggTrade" in stream:
                self._handle_agg_trade(data)
            elif "bookTicker" in stream:
                self._handle_book_ticker(data)
            elif "diffDepth" in stream:
                self._handle_depth(data)
        except Exception as e:
            logger.warning(f"Dispatch error on {stream}: {e}")

    async def run(self) -> None:
        self._running = True
        score_task = asyncio.create_task(self._score_loop())
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(WS_URL, heartbeat=30) as ws:
                    logger.info("Observer connected to Binance WebSocket")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._dispatch(data)
                            except json.JSONDecodeError:
                                pass
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning("WebSocket closed/error, reconnecting...")
                            break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Observer error: {e}")
        finally:
            self._running = False
            score_task.cancel()

    async def run_with_reconnect(self) -> None:
        while True:
            try:
                await self.run()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Observer crashed, reconnecting in 5s: {e}")
                await asyncio.sleep(5)


async def main():
    logging.basicConfig(level=logging.INFO)
    obs = Observer()
    await obs.run_with_reconnect()


if __name__ == "__main__":
    asyncio.run(main())
