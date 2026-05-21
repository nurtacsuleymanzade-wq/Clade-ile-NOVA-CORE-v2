import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent

SYMBOL = "BTCUSDT"
BINANCE_WS = "wss://stream.binance.com:9443"
BINANCE_REST = "https://api.binance.com"

SCORE_INTERVAL_MS = 1000
MAX_OPEN_TRADES = 10
MAX_SAME_DIRECTION = 5
MAX_SAME_PATTERN = 3
MIN_RR = 1.5
TRADE_TIMEOUT_HOURS = 4

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DATA_DIR = BASE_DIR / "data"
STATE_DIR = BASE_DIR / "state"
REPORTS_DIR = BASE_DIR / "reports"
LOGS_DIR = BASE_DIR / "logs"

SCORES_FILE = DATA_DIR / "1s_scores.jsonl"
CANDLE_DNA_FILE = DATA_DIR / "candle_dna.jsonl"
PAPER_TRADES_DB = DATA_DIR / "paper_trades.db"
CLOSED_TRADES_FILE = DATA_DIR / "closed_trades.jsonl"
EDGE_MATRIX_HISTORY_FILE = DATA_DIR / "edge_matrix_history.jsonl"

MICRO_EVENT_FILE = STATE_DIR / "latest_micro_event.json"
CANDLE_DNA_STATE_FILE = STATE_DIR / "latest_candle_dna.json"
PATTERN_FILE = STATE_DIR / "latest_pattern.json"
GEOMETRY_FILE = STATE_DIR / "latest_geometry.json"
DECISION_FILE = STATE_DIR / "latest_decision.json"
LIFECYCLE_FILE = STATE_DIR / "latest_lifecycle.json"
TREND_FILE = STATE_DIR / "latest_trend.json"
REGIME_FILE = STATE_DIR / "latest_regime.json"
ZONES_FILE = STATE_DIR / "latest_zones.json"
EDGE_MATRIX_FILE = STATE_DIR / "latest_edge_matrix.json"
SUPPRESSED_FILE = STATE_DIR / "suppressed_patterns.json"
REPORT_FILE = REPORTS_DIR / "latest_report.json"

safe_to_open_real_trade = False

LONDON_START_UTC = 8
LONDON_END_UTC = 11
NEW_YORK_START_UTC = 13
NEW_YORK_END_UTC = 16
