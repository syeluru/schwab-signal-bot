from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from signal_bot.models import OptionType, ResolvedOptionContract

_EXEC_DIR = Path(__file__).resolve().parent.parent / "6_execution"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_EXEC_DIR), str(_PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from broker_api.schwab_client import schwab_client  # noqa: E402


@dataclass(frozen=True)
class QuoteSnapshot:
    symbol: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    mark: Optional[float]
    raw: dict

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return round((self.bid + self.ask) / 2.0, 2)
        return self.mark

    @property
    def executable_sell_price(self) -> Optional[float]:
        if self.mid is not None:
            return round(max(self.mid - 0.10, 0.01), 2)
        if self.bid is not None:
            return self.bid
        if self.last is not None:
            return self.last
        return None


class SchwabSignalBotAdapter:
    def __init__(self, client=schwab_client):
        self.client = client

    async def resolve_option_contract(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: OptionType,
    ) -> ResolvedOptionContract:
        chain = await self.client.get_option_chain(
            symbol=underlying,
            contract_type=option_type.value,
            from_date=expiry,
            to_date=expiry,
            strike_range="ALL",
            strike_count=50,
            include_underlying_quote=True,
        )
        exp_map_key = "callExpDateMap" if option_type == OptionType.CALL else "putExpDateMap"
        exp_map = chain.get(exp_map_key) or {}
        if not exp_map:
            raise ValueError(f"No {option_type.value} option map returned for {underlying}")

        target_option = None
        for exp_key, strike_map in exp_map.items():
            if not exp_key.startswith(expiry.isoformat()):
                continue
            for strike_key, entries in strike_map.items():
                try:
                    parsed_strike = float(strike_key)
                except ValueError:
                    continue
                if abs(parsed_strike - strike) > 1e-6:
                    continue
                if not entries:
                    continue
                target_option = entries[0]
                break
            if target_option:
                break

        if not target_option:
            raise ValueError(
                f"Could not resolve {underlying} {expiry.isoformat()} {strike} {option_type.value} from chain"
            )

        return ResolvedOptionContract(
            symbol=target_option.get("symbol") or target_option.get("symbolId") or target_option["description"],
            description=target_option.get("description", ""),
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            bid=_to_float(target_option.get("bid")),
            ask=_to_float(target_option.get("ask")),
            last=_to_float(target_option.get("last")),
            mark=_derive_mark(target_option),
        )

    async def get_quote(self, symbol: str) -> QuoteSnapshot:
        raw = await self.client.get_quote(symbol)
        payload = _unwrap_quote_payload(raw, symbol)
        return QuoteSnapshot(
            symbol=symbol,
            bid=_extract_float(payload, "bidPrice", "bid", "bidPriceInDouble"),
            ask=_extract_float(payload, "askPrice", "ask", "askPriceInDouble"),
            last=_extract_float(payload, "lastPrice", "last", "lastPriceInDouble", "mark"),
            mark=_extract_float(payload, "mark", "markChange", "markPrice"),
            raw=payload,
        )

    async def place_option_buy_limit(self, option_symbol: str, quantity: int, limit_price: float) -> dict:
        order = {
            "orderType": "LIMIT",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "price": _fmt(limit_price),
            "orderLegCollection": [
                {
                    "instruction": "BUY_TO_OPEN",
                    "quantity": quantity,
                    "instrument": {"assetType": "OPTION", "symbol": option_symbol},
                }
            ],
        }
        return await self.client.place_order(order)

    async def place_option_sell_limit(self, option_symbol: str, quantity: int, limit_price: float) -> dict:
        order = {
            "orderType": "LIMIT",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "price": _fmt(limit_price),
            "orderLegCollection": [
                {
                    "instruction": "SELL_TO_CLOSE",
                    "quantity": quantity,
                    "instrument": {"assetType": "OPTION", "symbol": option_symbol},
                }
            ],
        }
        return await self.client.place_order(order)

    async def place_option_sell_market(self, option_symbol: str, quantity: int) -> dict:
        order = {
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": "SELL_TO_CLOSE",
                    "quantity": quantity,
                    "instrument": {"assetType": "OPTION", "symbol": option_symbol},
                }
            ],
        }
        return await self.client.place_order(order)

    async def get_order(self, order_id: str) -> dict:
        return await self.client.get_order(order_id)

    async def cancel_order(self, order_id: str) -> bool:
        return await self.client.cancel_order(order_id)

    async def wait_for_fill(
        self,
        order_id: str,
        timeout_seconds: int = 120,
        poll_interval: float = 2.0,
    ) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_order = None
        while asyncio.get_running_loop().time() < deadline:
            last_order = await self.get_order(order_id)
            status = str(last_order.get("status", "")).upper()
            if status == "FILLED":
                return {
                    "status": status,
                    "filled_quantity": _extract_filled_qty(last_order),
                    "avg_fill_price": _extract_avg_fill_price(last_order),
                    "raw": last_order,
                }
            if status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                return {
                    "status": status,
                    "filled_quantity": _extract_filled_qty(last_order),
                    "avg_fill_price": _extract_avg_fill_price(last_order),
                    "raw": last_order,
                }
            await asyncio.sleep(poll_interval)
        logger.warning(f"Timed out waiting for fill on order {order_id}")
        return {
            "status": str((last_order or {}).get("status", "TIMEOUT")).upper(),
            "filled_quantity": _extract_filled_qty(last_order or {}),
            "avg_fill_price": _extract_avg_fill_price(last_order or {}),
            "raw": last_order or {},
        }


def _unwrap_quote_payload(raw: dict, symbol: str) -> dict:
    if symbol in raw and isinstance(raw[symbol], dict):
        return raw[symbol]
    if raw:
        first_val = next(iter(raw.values()))
        if isinstance(first_val, dict):
            return first_val
    return raw


def _extract_float(payload: dict, *keys: str) -> Optional[float]:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return _to_float(payload[key])
    for nested_key in ("quote", "reference", "regular"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            for key in keys:
                if key in nested and nested[key] not in (None, ""):
                    return _to_float(nested[key])
    return None


def _derive_mark(payload: dict) -> Optional[float]:
    mark = _extract_float(payload, "mark", "markPrice")
    if mark is not None:
        return mark
    bid = _extract_float(payload, "bidPrice", "bid")
    ask = _extract_float(payload, "askPrice", "ask")
    if bid is not None and ask is not None:
        return round((bid + ask) / 2.0, 2)
    last = _extract_float(payload, "lastPrice", "last")
    return last


def _extract_avg_fill_price(order_payload: dict) -> Optional[float]:
    activities = order_payload.get("orderActivityCollection") or []
    qty_price_pairs: list[tuple[float, float]] = []
    for activity in activities:
        for leg_exec in activity.get("executionLegs", []) or []:
            price = _to_float(leg_exec.get("price"))
            qty = _to_float(leg_exec.get("quantity")) or 1.0
            if price is not None:
                qty_price_pairs.append((qty, price))
    if qty_price_pairs:
        total_qty = sum(qty for qty, _ in qty_price_pairs)
        if total_qty > 0:
            return round(sum(qty * price for qty, price in qty_price_pairs) / total_qty, 4)
    for key in ("filledAveragePrice", "price"):
        if key in order_payload and order_payload[key] not in (None, ""):
            return _to_float(order_payload[key])
    return None


def _extract_filled_qty(order_payload: dict) -> int:
    qty = order_payload.get("filledQuantity")
    if qty not in (None, ""):
        return int(float(qty))
    activities = order_payload.get("orderActivityCollection") or []
    total = 0
    for activity in activities:
        total += int(float(activity.get("quantity", 0) or 0))
        for leg_exec in activity.get("executionLegs", []) or []:
            total += int(float(leg_exec.get("quantity", 0) or 0))
    return total


def _to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(price: float) -> str:
    return f"{price:.2f}"
