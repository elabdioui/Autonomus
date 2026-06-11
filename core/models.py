"""Core dataclasses — SPEC 0 §4."""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Signal:
    signal_id: str
    ts_utc: datetime
    strategy: str           # "S1".."S4"
    direction: str          # "LONG" | "SHORT"
    killzone: str           # "LONDON" | "NY_AM" | "NY_PM" | "ASIA"
    entry_type: str         # "MARKET" | "LIMIT"
    entry_price: float
    entry_zone_low: float
    entry_zone_high: float
    sl: float
    tp1: float
    tp2: float
    sl_pips: float
    confluences: list[str] = field(default_factory=list)
    score: int = 0
    context: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        import json
        return {
            "signal_id": self.signal_id,
            "ts_utc": self.ts_utc.isoformat(),
            "strategy": self.strategy,
            "direction": self.direction,
            "killzone": self.killzone,
            "entry_type": self.entry_type,
            "entry_price": self.entry_price,
            "entry_zone_low": self.entry_zone_low,
            "entry_zone_high": self.entry_zone_high,
            "sl": self.sl,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "sl_pips": self.sl_pips,
            "score": self.score,
            "confluences": json.dumps(self.confluences),
            "context": json.dumps(self.context),
        }


@dataclass
class TradeRecord:
    trade_id: str
    signal_id: str
    mt5_ticket: int
    strategy: str
    direction: str
    lot: float
    entry_price_fill: float
    entry_ts_utc: datetime
    sl_initial: float
    sl_current: float
    tp1: float
    tp2: float
    status: str             # "PENDING" | "OPEN" | "PARTIAL" | "CLOSED" | "CANCELLED"
    exit_reason: str | None = None
    exit_ts_utc: datetime | None = None
    pnl_pips: float | None = None
    pnl_usd: float | None = None
    mae_pips: float = 0.0
    mfe_pips: float = 0.0
    news_flag: bool = False
    vol_regime: str = "normal"
    spread_at_entry_pips: float = 0.0

    def to_row(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "signal_id": self.signal_id,
            "mt5_ticket": self.mt5_ticket,
            "strategy": self.strategy,
            "direction": self.direction,
            "lot": self.lot,
            "entry_price_fill": self.entry_price_fill,
            "entry_ts_utc": self.entry_ts_utc.isoformat(),
            "sl_initial": self.sl_initial,
            "sl_current": self.sl_current,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "status": self.status,
            "exit_reason": self.exit_reason,
            "exit_ts_utc": self.exit_ts_utc.isoformat() if self.exit_ts_utc else None,
            "pnl_pips": self.pnl_pips,
            "pnl_usd": self.pnl_usd,
            "mae_pips": self.mae_pips,
            "mfe_pips": self.mfe_pips,
            "news_flag": int(self.news_flag),
            "vol_regime": self.vol_regime,
            "spread_at_entry_pips": self.spread_at_entry_pips,
        }

    @classmethod
    def from_row(cls, row: dict) -> "TradeRecord":
        from datetime import datetime
        def _dt(v):
            return datetime.fromisoformat(v) if v else None
        return cls(
            trade_id=row["trade_id"],
            signal_id=row["signal_id"],
            mt5_ticket=row["mt5_ticket"],
            strategy=row["strategy"],
            direction=row["direction"],
            lot=row["lot"],
            entry_price_fill=row["entry_price_fill"],
            entry_ts_utc=_dt(row["entry_ts_utc"]),
            sl_initial=row["sl_initial"],
            sl_current=row["sl_current"],
            tp1=row["tp1"],
            tp2=row["tp2"],
            status=row["status"],
            exit_reason=row["exit_reason"],
            exit_ts_utc=_dt(row["exit_ts_utc"]),
            pnl_pips=row["pnl_pips"],
            pnl_usd=row["pnl_usd"],
            mae_pips=row["mae_pips"] or 0.0,
            mfe_pips=row["mfe_pips"] or 0.0,
            news_flag=bool(row["news_flag"]),
            vol_regime=row["vol_regime"] or "normal",
            spread_at_entry_pips=row["spread_at_entry_pips"] or 0.0,
        )
