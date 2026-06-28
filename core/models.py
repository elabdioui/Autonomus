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
    setup: str = ""

    @property
    def sl_structural(self) -> float:
        return self.sl

    @property
    def tp_final(self) -> float:
        return self.tp2

    @property
    def meta(self) -> dict:
        return self.context

    def to_row(self) -> dict:
        import json
        return {
            "signal_id": self.signal_id,
            "ts_utc": self.ts_utc.isoformat(),
            "strategy": self.strategy,
            "setup": self.setup or self.strategy,
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
    signal_id: str | None   # None for crash-recovered positions
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
    setup: str = ""
    magic: int = 0
    lifecycle_state: str = "OPEN"
    sl_executed: float = 0.0
    tp_final: float = 0.0
    killzone: str = "OFF"
    htf_bias: str = "NEUTRAL"
    bias_aligned: bool = False
    news_red_active: str = "unknown"
    premium_discount: str = "EQ"
    exit_reason: str | None = None
    exit_ts_utc: datetime | None = None
    pnl_pips: float | None = None
    pnl_usd: float | None = None
    mae_pips: float = 0.0
    mfe_pips: float = 0.0
    news_flag: bool = False
    vol_regime: str = "normal"
    spread_at_entry_pips: float = 0.0
    sl_structural_pips: float = 0.0
    would_block_position: bool = False
    would_block_cooldown: bool = False
    would_block_news: bool = False
    would_block_spread: bool = False
    commission_usd: float = 0.0
    swap_usd: float = 0.0
    pnl_gross_usd: float | None = None
    pnl_net_usd: float | None = None
    tp1_hit: bool = False
    partial_close_price: float | None = None
    realized_r: float | None = None
    realized_r_net: float | None = None
    duration_s: int | None = None
    be_target: float | None = None   # queued BE price (persisted for crash recovery)
    be_retries: int = 0              # number of BE-modify attempts so far

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
            "setup": self.setup or self.strategy,
            "magic": self.magic,
            "lifecycle_state": self.lifecycle_state,
            "sl_executed": self.sl_executed or self.sl_initial,
            "tp_final": self.tp_final or self.tp2,
            "killzone": self.killzone,
            "htf_bias": self.htf_bias,
            "bias_aligned": int(self.bias_aligned),
            "news_red_active": self.news_red_active,
            "premium_discount": self.premium_discount,
            "exit_reason": self.exit_reason,
            "exit_ts_utc": self.exit_ts_utc.isoformat() if self.exit_ts_utc else None,
            "pnl_pips": self.pnl_pips,
            "pnl_usd": self.pnl_usd,
            "mae_pips": self.mae_pips,
            "mfe_pips": self.mfe_pips,
            "news_flag": int(self.news_flag),
            "vol_regime": self.vol_regime,
            "spread_at_entry_pips": self.spread_at_entry_pips,
            "sl_structural_pips": self.sl_structural_pips,
            "would_block_position": int(self.would_block_position),
            "would_block_cooldown": int(self.would_block_cooldown),
            "would_block_news": int(self.would_block_news),
            "would_block_spread": int(self.would_block_spread),
            "commission_usd": self.commission_usd,
            "swap_usd": self.swap_usd,
            "pnl_gross_usd": self.pnl_gross_usd,
            "pnl_net_usd": self.pnl_net_usd,
            "tp1_hit": int(self.tp1_hit),
            "partial_close_price": self.partial_close_price,
            "realized_r": self.realized_r,
            "realized_r_net": self.realized_r_net,
            "duration_s": self.duration_s,
            "be_target": self.be_target,
            "be_retries": self.be_retries,
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
            setup=row.get("setup") or row["strategy"],
            magic=row.get("magic") or 0,
            lifecycle_state=row.get("lifecycle_state") or (
                "CLOSED" if row["status"] == "CLOSED" else row["status"]
            ),
            sl_executed=row.get("sl_executed") or row["sl_initial"],
            tp_final=row.get("tp_final") or row["tp2"],
            killzone=row.get("killzone") or "OFF",
            htf_bias=row.get("htf_bias") or "NEUTRAL",
            bias_aligned=bool(row.get("bias_aligned") or 0),
            news_red_active=row.get("news_red_active") or "unknown",
            premium_discount=row.get("premium_discount") or "EQ",
            exit_reason=row["exit_reason"],
            exit_ts_utc=_dt(row["exit_ts_utc"]),
            pnl_pips=row["pnl_pips"],
            pnl_usd=row["pnl_usd"],
            mae_pips=row["mae_pips"] or 0.0,
            mfe_pips=row["mfe_pips"] or 0.0,
            news_flag=bool(row["news_flag"]),
            vol_regime=row["vol_regime"] or "normal",
            spread_at_entry_pips=row["spread_at_entry_pips"] or 0.0,
            sl_structural_pips=row.get("sl_structural_pips") or 0.0,
            would_block_position=bool(row.get("would_block_position") or 0),
            would_block_cooldown=bool(row.get("would_block_cooldown") or 0),
            would_block_news=bool(row.get("would_block_news") or 0),
            would_block_spread=bool(row.get("would_block_spread") or 0),
            commission_usd=row.get("commission_usd") or 0.0,
            swap_usd=row.get("swap_usd") or 0.0,
            pnl_gross_usd=row.get("pnl_gross_usd"),
            pnl_net_usd=row.get("pnl_net_usd"),
            tp1_hit=bool(row.get("tp1_hit") or 0),
            partial_close_price=row.get("partial_close_price"),
            realized_r=row.get("realized_r"),
            realized_r_net=row.get("realized_r_net"),
            duration_s=row.get("duration_s"),
            be_target=row.get("be_target"),
            be_retries=row.get("be_retries") or 0,
        )
