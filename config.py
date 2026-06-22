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
    MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "50"))
    SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "5"))

    ENABLED_STRATEGIES: list[str] = os.getenv("ENABLED_STRATEGIES", "S1,S2,S3,S4").split(",")
    ENABLED_KILLZONES: list[str] = os.getenv("ENABLED_KILLZONES", "LONDON,NY_AM,NY_PM,ASIA").split(",")

    # Risk / management (pips). TP1 is always exactly 1 executed R and the
    # ordinary runner is 2R; stale .env TP values cannot desynchronise them.
    SL_MAX_PIPS: float = float(os.getenv("SL_MAX_PIPS", "20"))
    TP1_PIPS: float = SL_MAX_PIPS
    TP2_PIPS: float = 2 * SL_MAX_PIPS
    SL_BUFFER_PIPS: float = float(os.getenv("SL_BUFFER_PIPS", "2"))
    PARTIAL_CLOSE_PCT: float = float(os.getenv("PARTIAL_CLOSE_PCT", "50"))
    TIMEOUT_MINUTES: int = int(os.getenv("TIMEOUT_MINUTES", "45"))
    COOLDOWN_MINUTES: int = int(os.getenv("COOLDOWN_MINUTES", "10"))
    PENDING_ORDER_EXPIRY_MIN: int = int(os.getenv("PENDING_ORDER_EXPIRY_MIN", "15"))
    MAX_SPREAD_PIPS: float = float(os.getenv("MAX_SPREAD_PIPS", "4.0"))

    # News tagging
    FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")

    TIMEZONE: str = os.getenv("TIMEZONE", "Africa/Casablanca")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
    # RotatingFileHandler count excludes the active file: 4 backups + active = 5.
    LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", "4"))
    MT5_CONNECT_RETRIES: int = int(os.getenv("MT5_CONNECT_RETRIES", "5"))
    MT5_RETRY_BASE_SECONDS: float = float(os.getenv("MT5_RETRY_BASE_SECONDS", "2"))
    MT5_RETRY_MAX_SECONDS: float = float(os.getenv("MT5_RETRY_MAX_SECONDS", "30"))
    DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8080"))
    EXCEL_EXPORT_PATH: str = os.getenv("EXCEL_EXPORT_PATH", r"data\scalper_journal.xlsx")

    # OHLC fetch counts
    OHLC_COUNT_M1: int = 300
    OHLC_COUNT_M5: int = 200
    OHLC_COUNT_M15: int = 100
    OHLC_COUNT_H1: int = 100
    OHLC_COUNT_H4: int = 100

    # Indicator params
    SWING_LOOKBACK: int = 5
    FVG_MIN_SIZE_PIPS: float = 3.0
    REGIME_ATR_PERIOD: int = 14
    REGIME_VOL_MULTIPLIER: float = 2.0
    REGIME_RANGE_MULTIPLIER: float = 0.5

    # SPEC D strategy hypotheses. They are deliberately environment-overridable
    # so the measurement cycle can tune them without code changes.
    S1_EQUAL_LEVEL_TOLERANCE_PIPS: float = float(os.getenv("S1_EQUAL_LEVEL_TOLERANCE_PIPS", "1.5"))
    S1_SWEEP_LOOKBACK_BARS: int = int(os.getenv("S1_SWEEP_LOOKBACK_BARS", "20"))
    S1_REQUIRE_CHOCH: bool = os.getenv("S1_REQUIRE_CHOCH", "false").lower() == "true"
    S1_SL_BUFFER_PIPS: float = float(os.getenv("S1_SL_BUFFER_PIPS", "2"))
    S1_COOLDOWN_SECONDS: int = int(os.getenv("S1_COOLDOWN_SECONDS", "0"))

    S2_COMPRESSION_BARS: int = int(os.getenv("S2_COMPRESSION_BARS", "10"))
    S2_MAX_RANGE_PIPS: float = float(os.getenv("S2_MAX_RANGE_PIPS", "15"))
    S2_DISPLACEMENT_BODY_RATIO: float = float(os.getenv("S2_DISPLACEMENT_BODY_RATIO", "1.5"))
    S2_SL_BUFFER_PIPS: float = float(os.getenv("S2_SL_BUFFER_PIPS", "2"))

    S3_EMA_PERIOD: int = int(os.getenv("S3_EMA_PERIOD", "20"))
    S3_EXTENSION_PIPS: float = float(os.getenv("S3_EXTENSION_PIPS", "25"))
    S3_WICK_REJECTION_RATIO: float = float(os.getenv("S3_WICK_REJECTION_RATIO", "0.6"))
    S3_SL_BUFFER_PIPS: float = float(os.getenv("S3_SL_BUFFER_PIPS", "2"))

    S4_TREND_EMA: int = int(os.getenv("S4_TREND_EMA", "50"))
    S4_BOS_LOOKBACK_BARS: int = int(os.getenv("S4_BOS_LOOKBACK_BARS", "30"))
    S4_REQUIRE_FVG_OR_OB: bool = os.getenv("S4_REQUIRE_FVG_OR_OB", "false").lower() == "true"
    S4_SL_BUFFER_PIPS: float = float(os.getenv("S4_SL_BUFFER_PIPS", "2"))
    S4_PULLBACK_TOLERANCE_PIPS: float = float(os.getenv("S4_PULLBACK_TOLERANCE_PIPS", "5"))

    # Friday flat: force-close all positions this many minutes before market close
    FRIDAY_FLAT_UTC: str = os.getenv("FRIDAY_FLAT_UTC", "21:50")

    # XAUUSDm: point=0.001, digits=3, 1 pip=10 points=0.01.
    POINT: float = 0.001
    DIGITS: int = 3
    PIP: float = 0.01


cfg = Config()
