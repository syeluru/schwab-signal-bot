from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from signal_bot.models import (
    EntryMode,
    OptionContractSpec,
    OptionType,
    ParsedSignal,
    RiskRules,
    TriggerRule,
)

_MONTH_LOOKUP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_CONTRACT_PATTERNS = [
    re.compile(
        r"(?:(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)\s+)?"
        r"(?P<underlying>[A-Z]{1,6})\s+"
        r"(?P<month>[A-Za-z]{3,4})\s*(?P<day>\d{1,2})(?:,?\s*(?P<year>\d{2,4}))?\s+"
        r"(?P<strike>\d+(?:\.\d+)?)\s*(?P<cp>[CP]|CALL|PUT)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)\s+)?"
        r"(?P<underlying>[A-Z]{1,6})\s+"
        r"(?P<day>\d{1,2})\s*(?P<month>[A-Za-z]{3,4})(?:,?\s*(?P<year>\d{2,4}))?\s+"
        r"(?P<strike>\d+(?:\.\d+)?)\s*(?P<cp>[CP]|CALL|PUT)\b",
        re.IGNORECASE,
    ),
]

_TRIGGER_VALUE_RE = re.compile(r"(?P<symbol>/[A-Z0-9]{1,6})[^\d-]*(?P<price>-?\d+(?:\.\d+)?)")
_STOP_RE = re.compile(r"(?P<pct>-?\d+(?:\.\d+)?)\s*%")
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?)\b", re.IGNORECASE)
_UPDATE_RE = re.compile(r"\*\*\s*UPDATE\s*\*\*", re.IGNORECASE)
_OTHER_TIMED_FUTURES_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s*/[A-Z0-9]{1,6}\b", re.IGNORECASE)


class SignalParseError(ValueError):
    pass


@dataclass
class _FieldState:
    contract: Optional[OptionContractSpec] = None
    contract_line_idx: Optional[int] = None
    signal_timestamp: Optional[datetime] = None
    aggro: Optional[TriggerRule] = None
    aggro_idx: Optional[int] = None
    moderate: Optional[TriggerRule] = None
    moderate_idx: Optional[int] = None
    stop_pct: Optional[float] = None
    stop_idx: Optional[int] = None
    t1_rule: Optional[TriggerRule] = None
    t1_idx: Optional[int] = None
    notes: list[str] = field(default_factory=list)
    trigger_family: Optional[str] = None


class SignalParser:
    def __init__(self, today: Optional[date] = None):
        self.today = today or date.today()

    def parse(self, raw_text: str) -> ParsedSignal:
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if not lines:
            raise SignalParseError("Signal is empty")

        base_start, contract = self._parse_contract_line(lines)
        update_starts = self._find_update_starts(lines[base_start + 1 :])
        if update_starts:
            update_starts = [base_start + 1 + idx for idx in update_starts]

        block_ranges: list[tuple[int, int, bool]] = []
        cur = base_start
        if not update_starts:
            block_ranges.append((base_start, len(lines), False))
        else:
            block_ranges.append((base_start, update_starts[0], False))
            for i, start in enumerate(update_starts):
                end = update_starts[i + 1] if i + 1 < len(update_starts) else len(lines)
                block_ranges.append((start, end, True))

        state = _FieldState(
            contract=contract,
            contract_line_idx=base_start,
            signal_timestamp=self._parse_timestamp(lines[base_start], contract.expiry),
        )
        for start, end, is_update in block_ranges:
            state = self._parse_block(lines[start:end], start, state, is_update=is_update)

        if state.contract is None:
            raise SignalParseError("Could not find contract line")
        if state.aggro is None:
            raise SignalParseError("Could not find aggro trigger line")
        if state.t1_rule is None:
            raise SignalParseError("Could not find T1 scaleout line")

        rules = RiskRules(
            # Default stop is always 25% below the original entry debit unless
            # the signal explicitly provides a StopLoss percent.
            initial_stop_pct=abs(state.stop_pct) / 100.0 if state.stop_pct is not None else 0.25,
        )
        entry_modes = {EntryMode.AGGRO: state.aggro}
        if state.moderate is not None:
            entry_modes[EntryMode.MODERATE] = state.moderate
        return ParsedSignal(
            raw_text=raw_text,
            signal_timestamp=state.signal_timestamp,
            contract=state.contract,
            notes=state.notes,
            entry_modes=entry_modes,
            t1_rule=state.t1_rule,
            risk_rules=rules,
        )

    def _find_update_starts(self, lines: list[str]) -> list[int]:
        starts = []
        for idx, line in enumerate(lines):
            if _UPDATE_RE.search(line):
                starts.append(idx)
        return starts

    def _parse_block(self, lines: list[str], absolute_start: int, state: _FieldState, *, is_update: bool) -> _FieldState:
        local_state = _FieldState(**{**state.__dict__})
        for offset, raw_line in enumerate(lines):
            idx = absolute_start + offset
            line = raw_line.strip()
            if not line:
                continue

            if is_update and offset == 0 and _UPDATE_RE.search(line):
                contract = self._parse_contract_from_update_line(line)
                if contract and local_state.contract and not self._contracts_match(local_state.contract, contract):
                    raise SignalParseError("Update contract does not match original signal contract")
                if contract and local_state.signal_timestamp is None:
                    local_state.signal_timestamp = self._parse_timestamp(line, contract.expiry)
                continue

            maybe_contract = self._parse_contract_line_from_text(line)
            if maybe_contract:
                if local_state.contract is None:
                    local_state.contract = maybe_contract
                    local_state.contract_line_idx = idx
                    local_state.signal_timestamp = self._parse_timestamp(line, maybe_contract.expiry)
                continue

            if self._looks_like_unrelated_timed_futures_line(line):
                continue

            if self._is_aggro_line(line):
                trig = self._parse_trigger_line(line)
                if trig and self._accept_trigger(local_state, trig):
                    local_state.aggro = trig
                    local_state.aggro_idx = idx
                    local_state.trigger_family = trig.trigger_symbol
                    continue

            if self._is_moderate_line(line):
                trig = self._parse_trigger_line(line)
                if trig and self._accept_trigger(local_state, trig):
                    local_state.moderate = trig
                    if local_state.trigger_family is None:
                        local_state.trigger_family = trig.trigger_symbol
                    local_state.moderate_idx = idx
                    continue

            if self._is_t1_line(line):
                trig = self._parse_trigger_line(line)
                if trig and self._accept_trigger(local_state, trig):
                    local_state.t1_rule = trig
                    if local_state.trigger_family is None:
                        local_state.trigger_family = trig.trigger_symbol
                    local_state.t1_idx = idx
                    continue

            if self._is_stop_line(line):
                match = _STOP_RE.search(line)
                if match:
                    local_state.stop_pct = float(match.group("pct"))
                    local_state.stop_idx = idx
                    continue

            if not _UPDATE_RE.search(line):
                local_state.notes.append(line)

        return local_state

    def _accept_trigger(self, state: _FieldState, trig: TriggerRule) -> bool:
        if state.trigger_family is None:
            return True
        return trig.trigger_symbol.upper() == state.trigger_family.upper()

    def _looks_like_unrelated_timed_futures_line(self, line: str) -> bool:
        return bool(_OTHER_TIMED_FUTURES_RE.search(line))

    def _is_aggro_line(self, line: str) -> bool:
        lowered = line.lower()
        return any(alias in lowered for alias in ("aggro", "agro", "aggres", "very aggro"))

    def _is_moderate_line(self, line: str) -> bool:
        lowered = line.lower()
        return any(alias in lowered for alias in ("moderate", "modrate", "moderete", "moderat"))

    def _is_t1_line(self, line: str) -> bool:
        lowered = line.lower()
        return "t1" in lowered and ("scaleout" in lowered or "scale out" in lowered or "/" in lowered)

    def _is_stop_line(self, line: str) -> bool:
        lowered = line.lower()
        return any(token in lowered for token in ("stop", "sl", "stop loss", "stoploss", "stp"))

    def _parse_contract_line(self, lines: list[str]) -> tuple[int, OptionContractSpec]:
        for idx, line in enumerate(lines):
            contract = self._parse_contract_line_from_text(line)
            if contract:
                return idx, contract
        raise SignalParseError("Could not find contract line")

    def _parse_contract_from_update_line(self, line: str) -> Optional[OptionContractSpec]:
        cleaned = _UPDATE_RE.sub("", line).strip()
        return self._parse_contract_line_from_text(cleaned)

    def _parse_contract_line_from_text(self, line: str) -> Optional[OptionContractSpec]:
        for pattern in _CONTRACT_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            month = _MONTH_LOOKUP[match.group("month").lower()]
            day = int(match.group("day"))
            year = self._resolve_year(match.group("year"), month, day)
            cp_raw = match.group("cp").upper()
            option_type = OptionType.CALL if cp_raw in {"C", "CALL"} else OptionType.PUT
            return OptionContractSpec(
                underlying=match.group("underlying").upper(),
                expiry=date(year, month, day),
                strike=float(match.group("strike")),
                option_type=option_type,
            )
        return None

    def _contracts_match(self, a: OptionContractSpec, b: OptionContractSpec) -> bool:
        return (
            a.underlying == b.underlying
            and a.expiry == b.expiry
            and abs(a.strike - b.strike) < 1e-9
            and a.option_type == b.option_type
        )

    def _resolve_year(self, year_raw: Optional[str], month: int, day: int) -> int:
        if year_raw:
            year = int(year_raw)
            return 2000 + year if year < 100 else year
        candidate = date(self.today.year, month, day)
        if candidate < self.today:
            return self.today.year + 1
        return self.today.year

    def _parse_timestamp(self, line: str, base_date: date) -> Optional[datetime]:
        m = _TIME_RE.search(line)
        if not m:
            return None
        time_raw = m.group(1).upper().replace(" ", "")
        for fmt in ("%I:%M%p", "%I:%M:%S%p", "%H:%M", "%H:%M:%S"):
            try:
                parsed_t = datetime.strptime(time_raw, fmt).time()
                return datetime.combine(base_date, parsed_t)
            except ValueError:
                continue
        return None

    def _parse_trigger_line(self, line: str) -> Optional[TriggerRule]:
        match = _TRIGGER_VALUE_RE.search(line.upper())
        if not match:
            return None
        return TriggerRule(trigger_symbol=match.group("symbol"), trigger_price=float(match.group("price")))
