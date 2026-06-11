import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")


class Config:
    MT5_LOGIN: int = int(os.getenv("MT5_LOGIN", "0"))
    MT5_PASSWORD: str = os.getenv("MT5_PASSWORD", "")
    MT5_SERVER: str = os.getenv("MT5_SERVER", "")
    MT5_TERMINAL_PATH: str = os.getenv("MT5_TERMINAL_PATH", "")

    SYMBOL: str = os.getenv("SYMBOL", "XAUUSDm")
    LOT: float = float(os.getenv("LOT", "0.2"))
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "1"))
    SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "5"))

    ENABLED_STRATEGIES: list[str] = os.getenv("ENABLED_STRATEGIES", "S1,S2,S3,S4").split(",")
    ENABLED_KILLZONES: list[str] = os.getenv("ENABLED_KILLZONES", "LONDON,NY_AM,NY_PM,ASIA").split(",")

    # Risk / management (pips)
    TP1_PIPS: float = float(os.getenv("TP1_PIPS", "10"))
    TP2_PIPS: float = float(os.getenv("TP2_PIPS", "20"))
    SL_MAX_PIPS: float = float(os.getenv("SL_MAX_PIPS", "20"))
    SL_BUFFER_PIPS: float = float(os.getenv("SL_BUFFER_PIPS", "3"))
    PARTIAL_CLOSE_PCT: float = float(os.getenv("PARTIAL_CLOSE_PCT", "50"))
    TIMEOUT_MINUTES: int = int(os.getenv("TIMEOUT_MINUTES", "45"))
    COOLDOWN_MINUTES: int = int(os.getenv("COOLDOWN_MINUTES", "10"))
    PENDING_ORDER_EXPIRY_MIN: int = int(os.getenv("PENDING_ORDER_EXPIRY_MIN", "15"))
    MAX_SPREAD_PIPS: float = float(os.getenv("MAX_SPREAD_PIPS", "4.0"))

    # Per-strategy TP overrides
    S3_TP1_PIPS: float = float(os.getenv("S3_TP1_PIPS", "8"))
    S3_TP2_PIPS: float = float(os.getenv("S3_TP2_PIPS", "12"))

    # News tagging
    FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")

    TIMEZONE: str = os.getenv("TIMEZONE", "Africa/Casablanca")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8080"))
    EXCEL_EXPORT_PATH: str = os.getenv("EXCEL_EXPORT_PATH", r"data\scalper_journal.xlsx")

    # OHLC fetch counts
    OHLC_COUNT_M1: int = 300
    OHLC_COUNT_M5: int = 200
    OHLC_COUNT_M15: int = 100
    OHLC_COUNT_H1: int = 100

    # Indicator params
    SWING_LOOKBACK: int = 5
    FVG_MIN_SIZE_PIPS: float = 3.0
    REGIME_ATR_PERIOD: int = 14
    REGIME_VOL_MULTIPLIER: float = 2.0
    REGIME_RANGE_MULTIPLIER: float = 0.5

    # S4 SFP params
    SFP_VOLUME_LOOKBACK: int = 10
    SFP_VOLUME_FACTOR: float = 1.0
    OTE_LOW: float = 0.618
    OTE_HIGH: float = 0.786

    # S3 Bollinger params
    BB_PERIOD: int = 20
    BB_STD: float = 2.5
    S3_SL_MAX_PIPS: float = 15.0  # S3-specific hard cap (stricter than global)

    # Pip definition: 1 pip = 0.10 USD price movement on XAUUSD
    PIP: float = 0.10


cfg = Config()
