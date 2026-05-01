from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Optional


class EntryMode(str, Enum):
    AGGRO = "aggro"
    MODERATE = "moderate"


class OptionType(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class TradeStatus(str, Enum):
    PARSED = "PARSED"
    WAITING_FOR_MODE = "WAITING_FOR_MODE"
    WAITING_FOR_ENTRY = "WAITING_FOR_ENTRY"
    WAITING_FOR_ENTRY_CONFIRMATION = "WAITING_FOR_ENTRY_CONFIRMATION"
    ENTRY_ORDER_WORKING = "ENTRY_ORDER_WORKING"
    OPEN_FULL = "OPEN_FULL"
    OPEN_AFTER_T1 = "OPEN_AFTER_T1"
    OPEN_AFTER_T2 = "OPEN_AFTER_T2"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class TriggerRule:
    trigger_symbol: str
    trigger_price: float


@dataclass(frozen=True)
class OptionContractSpec:
    underlying: str
    expiry: date
    strike: float
    option_type: OptionType


@dataclass(frozen=True)
class RiskRules:
    initial_stop_pct: float = 0.25
    t1_scaleout_pct_of_original: float = 0.50
    t2_scaleout_pct_of_original: float = 0.25
    t3_scaleout_pct_of_original: float = 0.25
    t2_profit_multiple: float = 2.0
    t3_profit_multiple: float = 3.0
    post_t1_stop_offset: float = 0.10
    force_exit_time_local: time = time(15, 50)
    entry_cutoff_time_local: time = time(10, 35)
    confirmation_timeout_seconds: int = 180


@dataclass(frozen=True)
class ParsedSignal:
    raw_text: str
    signal_timestamp: Optional[datetime]
    contract: OptionContractSpec
    notes: list[str]
    entry_modes: dict[EntryMode, TriggerRule]
    t1_rule: TriggerRule
    risk_rules: RiskRules = field(default_factory=RiskRules)


@dataclass(frozen=True)
class ResolvedOptionContract:
    symbol: str
    description: str
    underlying: str
    expiry: date
    strike: float
    option_type: OptionType
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    mark: Optional[float] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return round((self.bid + self.ask) / 2.0, 2)
        return self.mark


@dataclass
class PositionState:
    quantity_original: int
    quantity_open: int
    entry_debit: Optional[float] = None
    initial_stop_price: Optional[float] = None
    active_stop_price: Optional[float] = None
    t2_target_price: Optional[float] = None
    t3_target_price: Optional[float] = None
    t1_fill_credit: Optional[float] = None


@dataclass
class TradeRuntime:
    trade_id: str
    signal: ParsedSignal
    mode_selected: EntryMode
    contract: ResolvedOptionContract
    quantity_original: int
    status: TradeStatus = TradeStatus.PARSED
    quantity_open: int = 0
    entry_trigger_hit_at: Optional[datetime] = None
    entry_order_id: Optional[str] = None
    entry_fill_time: Optional[datetime] = None
    entry_debit: Optional[float] = None
    initial_stop_price: Optional[float] = None
    active_stop_price: Optional[float] = None
    t1_done: bool = False
    t1_order_id: Optional[str] = None
    t1_fill_time: Optional[datetime] = None
    t1_fill_credit: Optional[float] = None
    t2_done: bool = False
    t2_order_id: Optional[str] = None
    t2_fill_time: Optional[datetime] = None
    t3_done: bool = False
    t3_order_id: Optional[str] = None
    t3_fill_time: Optional[datetime] = None
    t2_target_price: Optional[float] = None
    t3_target_price: Optional[float] = None
    expiry_force_exit_datetime: Optional[datetime] = None
    audit_log: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        self.audit_log.append(message)

    def t1_qty(self) -> int:
        return int(self.quantity_original * self.signal.risk_rules.t1_scaleout_pct_of_original)

    def t2_qty(self) -> int:
        return int(self.quantity_original * self.signal.risk_rules.t2_scaleout_pct_of_original)

    def t3_qty(self) -> int:
        return self.quantity_original - self.t1_qty() - self.t2_qty()
