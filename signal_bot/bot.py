from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

from signal_bot.models import EntryMode, ParsedSignal, TradeRuntime, TradeStatus
from signal_bot.asset_mapping import validate_underlying_trigger_pair
from signal_bot.parser import SignalParser
from signal_bot.schwab_adapter import QuoteSnapshot, SchwabSignalBotAdapter
from signal_bot.futures_stream import FuturesStreamMonitor


@dataclass
class SignalBotConfig:
    market_timezone: str = "America/New_York"
    poll_interval_seconds: float = 2.0
    quote_error_backoff_seconds: float = 10.0
    order_fill_timeout_seconds: int = 120
    confirmation_timeout_seconds: int = 180
    runtime_dir: Path = Path("runtime/signal_bot")
    logs_dir: Path = Path("logs/signal_bot")
    default_quantity: int = 4
    sell_limit_offset: float = 0.10


class SignalDrivenOptionBot:
    def __init__(self, broker: Optional[SchwabSignalBotAdapter] = None, config: Optional[SignalBotConfig] = None):
        self.broker = broker or SchwabSignalBotAdapter()
        self.config = config or SignalBotConfig()
        self.tz = ZoneInfo(self.config.market_timezone)
        self.futures_stream = FuturesStreamMonitor()
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        logger.add(self.config.logs_dir / "signal_bot.log", rotation="10 MB", enqueue=False)

    async def run_signal(self, raw_signal: str, quantity: Optional[int] = None, mode: Optional[EntryMode] = None) -> TradeRuntime:
        qty = quantity or self.config.default_quantity
        if qty < 4 or qty % 4 != 0:
            raise ValueError("Quantity must be a multiple of 4 and at least 4")

        parser = SignalParser(today=self.now_market().date())
        signal = parser.parse(raw_signal)
        for entry_rule in signal.entry_modes.values():
            validate_underlying_trigger_pair(signal.contract.underlying, entry_rule.trigger_symbol)
        validate_underlying_trigger_pair(signal.contract.underlying, signal.t1_rule.trigger_symbol)

        selected_mode = mode or await self.prompt_for_mode(signal)
        if selected_mode not in signal.entry_modes:
            available = ", ".join(m.value for m in signal.entry_modes)
            raise ValueError(f"Selected mode '{selected_mode.value}' is not present in signal. Available mode(s): {available}")

        contract = await self.broker.resolve_option_contract(
            underlying=signal.contract.underlying,
            expiry=signal.contract.expiry,
            strike=signal.contract.strike,
            option_type=signal.contract.option_type,
        )

        runtime = TradeRuntime(
            trade_id=str(uuid.uuid4()),
            signal=signal,
            mode_selected=selected_mode,
            contract=contract,
            quantity_original=qty,
            quantity_open=0,
            status=TradeStatus.WAITING_FOR_ENTRY,
        )
        runtime.expiry_force_exit_datetime = self._at_market_time(
            signal.contract.expiry,
            signal.risk_rules.force_exit_time_local,
        )
        self._log(runtime, "signal_parsed")
        self.print_summary(runtime)
        await self.monitor_for_entry(runtime)
        self._persist_runtime(runtime)
        return runtime

    async def prompt_for_mode(self, signal: ParsedSignal) -> EntryMode:
        aggro = signal.entry_modes[EntryMode.AGGRO]
        op = self._futures_trigger_operator(signal)
        if EntryMode.MODERATE not in signal.entry_modes:
            print(f"Only aggro mode found in signal: {aggro.trigger_symbol} {op} {aggro.trigger_price}")
            return EntryMode.AGGRO

        moderate = signal.entry_modes[EntryMode.MODERATE]
        prompt = (
            f"Select mode [aggro/moderate]\\n"
            f"  aggro    -> {aggro.trigger_symbol} {op} {aggro.trigger_price}\\n"
            f"  moderate -> {moderate.trigger_symbol} {op} {moderate.trigger_price}\\n> "
        )
        while True:
            raw = (await self._async_input(prompt)).strip().lower()
            if raw in {"aggro", "a"}:
                return EntryMode.AGGRO
            if raw in {"moderate", "m", "mod"}:
                return EntryMode.MODERATE
            print("Please type 'aggro' or 'moderate'.")

    def print_summary(self, runtime: TradeRuntime) -> None:
        signal = runtime.signal
        entry_rule = signal.entry_modes[runtime.mode_selected]
        op = self._futures_trigger_operator(signal)
        print("\n=== Parsed Signal Summary ===")
        print(f"Trade ID: {runtime.trade_id}")
        print(f"Contract: {signal.contract.underlying} {signal.contract.expiry:%b %d %Y} {signal.contract.strike:g} {signal.contract.option_type.value}")
        print(f"Resolved contract symbol: {runtime.contract.symbol}")
        print(f"Mode selected: {runtime.mode_selected.value}")
        print(f"Entry trigger: {entry_rule.trigger_symbol} {op} {entry_rule.trigger_price}")
        print(f"T1 trigger: {signal.t1_rule.trigger_symbol} {op} {signal.t1_rule.trigger_price}")
        print(f"Initial stop: {signal.risk_rules.initial_stop_pct * 100:.0f}% below entry debit")
        print(f"T2 target: {signal.risk_rules.t2_profit_multiple:.1f}x debit")
        print(f"T3 target: {signal.risk_rules.t3_profit_multiple:.1f}x debit")
        print(f"Entry cutoff ({self.config.market_timezone}): {signal.risk_rules.entry_cutoff_time_local.strftime('%I:%M %p')}")
        print(f"Expiry forced exit ({self.config.market_timezone}): {runtime.expiry_force_exit_datetime:%Y-%m-%d %I:%M %p %Z}")
        print(f"Quantity: {runtime.quantity_original}")
        if signal.notes:
            print("Notes:")
            for note in signal.notes:
                print(f"  - {note}")
        print("=============================\n")

    async def monitor_for_entry(self, runtime: TradeRuntime) -> None:
        entry_rule = runtime.signal.entry_modes[runtime.mode_selected]
        cutoff_dt = self._at_market_time(self.now_market().date(), runtime.signal.risk_rules.entry_cutoff_time_local)
        runtime.status = TradeStatus.WAITING_FOR_ENTRY
        self._log(runtime, f"entry_monitor_started cutoff={cutoff_dt.isoformat()}")
        last_seen = None
        futures_symbols = sorted({entry_rule.trigger_symbol, runtime.signal.t1_rule.trigger_symbol})
        await self.futures_stream.start(futures_symbols)
        try:
            while self.now_market() <= cutoff_dt:
                fut_quote = await self.futures_stream.get_latest(entry_rule.trigger_symbol, timeout=5.0)
                trigger_px = self._decision_price(fut_quote) if fut_quote else None
                if trigger_px is None:
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue
                last_seen = trigger_px
                if self._futures_trigger_hit(runtime.signal, trigger_px, entry_rule.trigger_price):
                    runtime.entry_trigger_hit_at = self.now_market()
                    runtime.status = TradeStatus.WAITING_FOR_ENTRY_CONFIRMATION
                    self._log(runtime, f"entry_trigger_hit futures_price={trigger_px}")
                    confirmed = await self.request_entry_confirmation(runtime, fut_quote, cutoff_dt)
                    if not confirmed:
                        runtime.status = TradeStatus.CANCELLED
                        self._log(runtime, "entry_not_confirmed")
                        return
                    await self.place_entry(runtime)
                    return
                await asyncio.sleep(self.config.poll_interval_seconds)
            runtime.status = TradeStatus.EXPIRED
            self._log(runtime, f"entry_window_expired last_seen={last_seen}")
            print(f"Entry window expired at {cutoff_dt:%I:%M %p %Z}; no trade entered.")
        finally:
            if runtime.status in {TradeStatus.EXPIRED, TradeStatus.CANCELLED, TradeStatus.ERROR}:
                await self.futures_stream.stop()

    async def request_entry_confirmation(self, runtime: TradeRuntime, fut_quote: QuoteSnapshot, cutoff_dt: datetime) -> bool:
        opt_quote = await self.broker.get_quote(runtime.contract.symbol)
        proposed_entry = opt_quote.ask or opt_quote.mid or opt_quote.last or opt_quote.mark
        print("\a")
        print("Entry trigger HIT")
        print(f"  Contract: {runtime.contract.symbol}")
        print(f"  Mode: {runtime.mode_selected.value}")
        print(f"  Futures: {fut_quote.symbol} last={fut_quote.last} mid={fut_quote.mid}")
        print(f"  Option: bid={opt_quote.bid} ask={opt_quote.ask} mid={opt_quote.mid} last={opt_quote.last}")
        print(f"  Quantity: {runtime.quantity_original}")
        print(f"  Proposed entry price: {proposed_entry}")
        print("Press Enter to confirm entry. Type anything else then Enter to skip.")
        seconds_left = max(1.0, min(
            runtime.signal.risk_rules.confirmation_timeout_seconds,
            (cutoff_dt - self.now_market()).total_seconds(),
            self.config.confirmation_timeout_seconds,
        ))
        try:
            response = await asyncio.wait_for(self._async_input("confirm> "), timeout=seconds_left)
        except asyncio.TimeoutError:
            print("Confirmation timed out.")
            return False
        return response.strip() in {"", "y", "yes"}

    async def place_entry(self, runtime: TradeRuntime) -> None:
        quote = await self.broker.get_quote(runtime.contract.symbol)
        entry_limit = quote.ask or quote.mid or quote.last or quote.mark
        if entry_limit is None:
            runtime.status = TradeStatus.ERROR
            raise RuntimeError("Could not determine entry price from option quote")

        runtime.status = TradeStatus.ENTRY_ORDER_WORKING
        order_resp = await self.broker.place_option_buy_limit(runtime.contract.symbol, runtime.quantity_original, entry_limit)
        order_id = str(order_resp.get("orderId") or order_resp.get("id") or "")
        if not order_id:
            runtime.status = TradeStatus.ERROR
            raise RuntimeError(f"No orderId returned from Schwab: {order_resp}")

        runtime.entry_order_id = order_id
        self._log(runtime, f"entry_order_placed order_id={order_id} limit={entry_limit}")
        fill = await self.broker.wait_for_fill(order_id, timeout_seconds=self.config.order_fill_timeout_seconds)
        if fill.get("status") != "FILLED":
            runtime.status = TradeStatus.CANCELLED
            self._log(runtime, f"entry_not_filled status={fill.get('status')}")
            raise RuntimeError(f"Entry order {order_id} did not fill: {fill.get('status')}")

        entry_debit = fill.get("avg_fill_price") or entry_limit
        runtime.entry_fill_time = self.now_market()
        runtime.entry_debit = entry_debit
        runtime.quantity_open = runtime.quantity_original
        runtime.initial_stop_price = round(entry_debit * (1.0 - runtime.signal.risk_rules.initial_stop_pct), 2)
        runtime.active_stop_price = runtime.initial_stop_price
        runtime.t2_target_price = round(entry_debit * runtime.signal.risk_rules.t2_profit_multiple, 2)
        runtime.t3_target_price = round(entry_debit * runtime.signal.risk_rules.t3_profit_multiple, 2)
        runtime.status = TradeStatus.OPEN_FULL
        self._log(
            runtime,
            f"entry_filled debit={entry_debit} stop={runtime.active_stop_price} t2={runtime.t2_target_price} t3={runtime.t3_target_price}",
        )
        print(f"Entry filled at {entry_debit:.2f}. Monitoring exits...")
        await self.manage_open_trade(runtime)

    async def manage_open_trade(self, runtime: TradeRuntime) -> None:
        consecutive_quote_errors = 0
        while runtime.status not in {TradeStatus.CLOSED, TradeStatus.CANCELLED, TradeStatus.ERROR}:
            now = self.now_market()
            if now >= runtime.expiry_force_exit_datetime:
                await self._force_exit(runtime, reason="expiry_time_exit")
                break

            if runtime.status == TradeStatus.OPEN_FULL:
                fut_quote = await self.futures_stream.get_latest(runtime.signal.t1_rule.trigger_symbol, timeout=5.0)
                fut_px = self._decision_price(fut_quote) if fut_quote else None
                if fut_px is not None and self._futures_trigger_hit(
                    runtime.signal,
                    fut_px,
                    runtime.signal.t1_rule.trigger_price,
                ):
                    await self._execute_t1(runtime)
                    continue

            option_quote = await self._get_quote_with_backoff(runtime, runtime.contract.symbol, consecutive_quote_errors)
            if option_quote is None:
                consecutive_quote_errors += 1
                continue
            consecutive_quote_errors = 0

            option_px = self._decision_price(option_quote)
            if option_px is None:
                await asyncio.sleep(self.config.poll_interval_seconds)
                continue

            if runtime.active_stop_price is not None and option_px <= runtime.active_stop_price:
                await self._force_exit(runtime, reason=f"stop_hit option_price={option_px}")
                break

            if runtime.status == TradeStatus.OPEN_AFTER_T1:
                if runtime.t2_target_price is not None and option_px >= runtime.t2_target_price:
                    await self._execute_t2(runtime)
                else:
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue

            elif runtime.status == TradeStatus.OPEN_AFTER_T2:
                if runtime.t3_target_price is not None and option_px >= runtime.t3_target_price:
                    await self._execute_t3(runtime)
                    break
                await asyncio.sleep(self.config.poll_interval_seconds)

            else:
                await asyncio.sleep(self.config.poll_interval_seconds)

    async def _get_quote_with_backoff(
        self,
        runtime: TradeRuntime,
        symbol: str,
        consecutive_errors: int,
    ) -> Optional[QuoteSnapshot]:
        try:
            return await self.broker.get_quote(symbol)
        except Exception as exc:
            wait_seconds = min(
                self.config.quote_error_backoff_seconds * (consecutive_errors + 1),
                60.0,
            )
            self._log(
                runtime,
                f"quote_error symbol={symbol!r} error={type(exc).__name__}: {exc}; "
                f"backing_off={wait_seconds:.1f}s",
            )
            await asyncio.sleep(wait_seconds)
            return None

    async def _execute_t1(self, runtime: TradeRuntime) -> None:
        qty = runtime.t1_qty()
        quote = await self.broker.get_quote(runtime.contract.symbol)
        limit_price = quote.executable_sell_price
        if limit_price is None:
            raise RuntimeError("Could not determine T1 limit price")
        resp = await self.broker.place_option_sell_limit(runtime.contract.symbol, qty, limit_price)
        order_id = str(resp.get("orderId") or "")
        runtime.t1_order_id = order_id
        self._log(runtime, f"t1_order_placed order_id={order_id} qty={qty} limit={limit_price}")
        fill = await self.broker.wait_for_fill(order_id, timeout_seconds=self.config.order_fill_timeout_seconds)
        if fill.get("status") != "FILLED":
            raise RuntimeError(f"T1 order not filled: {fill}")
        fill_credit = fill.get("avg_fill_price") or limit_price
        runtime.t1_done = True
        runtime.t1_fill_time = self.now_market()
        runtime.t1_fill_credit = fill_credit
        runtime.quantity_open -= qty
        runtime.active_stop_price = round((runtime.entry_debit or 0) + runtime.signal.risk_rules.post_t1_stop_offset, 2)
        runtime.status = TradeStatus.OPEN_AFTER_T1
        self._log(runtime, f"t1_filled fill={fill_credit} new_stop={runtime.active_stop_price} qty_open={runtime.quantity_open}")
        print(f"T1 filled at {fill_credit:.2f}; new stop is {runtime.active_stop_price:.2f}")

    async def _execute_t2(self, runtime: TradeRuntime) -> None:
        qty = runtime.t2_qty()
        quote = await self.broker.get_quote(runtime.contract.symbol)
        limit_price = quote.executable_sell_price
        if limit_price is None:
            raise RuntimeError("Could not determine T2 limit price")
        resp = await self.broker.place_option_sell_limit(runtime.contract.symbol, qty, limit_price)
        order_id = str(resp.get("orderId") or "")
        runtime.t2_order_id = order_id
        self._log(runtime, f"t2_order_placed order_id={order_id} qty={qty} limit={limit_price}")
        fill = await self.broker.wait_for_fill(order_id, timeout_seconds=self.config.order_fill_timeout_seconds)
        if fill.get("status") != "FILLED":
            raise RuntimeError(f"T2 order not filled: {fill}")
        runtime.t2_done = True
        runtime.t2_fill_time = self.now_market()
        runtime.quantity_open -= qty
        runtime.active_stop_price = runtime.t1_fill_credit
        runtime.status = TradeStatus.OPEN_AFTER_T2
        self._log(runtime, f"t2_filled new_stop={runtime.active_stop_price} qty_open={runtime.quantity_open}")
        print(f"T2 filled; new stop is {runtime.active_stop_price:.2f}")

    async def _execute_t3(self, runtime: TradeRuntime) -> None:
        qty = runtime.quantity_open
        quote = await self.broker.get_quote(runtime.contract.symbol)
        limit_price = quote.executable_sell_price
        if limit_price is None:
            raise RuntimeError("Could not determine T3 limit price")
        resp = await self.broker.place_option_sell_limit(runtime.contract.symbol, qty, limit_price)
        order_id = str(resp.get("orderId") or "")
        runtime.t3_order_id = order_id
        self._log(runtime, f"t3_order_placed order_id={order_id} qty={qty} limit={limit_price}")
        fill = await self.broker.wait_for_fill(order_id, timeout_seconds=self.config.order_fill_timeout_seconds)
        if fill.get("status") != "FILLED":
            raise RuntimeError(f"T3 order not filled: {fill}")
        runtime.t3_done = True
        runtime.t3_fill_time = self.now_market()
        runtime.quantity_open = 0
        runtime.status = TradeStatus.CLOSED
        self._log(runtime, "t3_filled trade_closed")
        print("T3 filled; trade closed.")
        await self.futures_stream.stop()

    async def _force_exit(self, runtime: TradeRuntime, reason: str) -> None:
        qty = runtime.quantity_open
        if qty <= 0:
            runtime.status = TradeStatus.CLOSED
            return
        resp = await self.broker.place_option_sell_market(runtime.contract.symbol, qty)
        order_id = str(resp.get("orderId") or "")
        self._log(runtime, f"force_exit_placed order_id={order_id} qty={qty} reason={reason}")
        fill = await self.broker.wait_for_fill(order_id, timeout_seconds=self.config.order_fill_timeout_seconds)
        runtime.quantity_open = 0
        runtime.status = TradeStatus.CLOSED
        self._log(runtime, f"force_exit_done status={fill.get('status')} reason={reason}")
        print(f"Force exit completed: {reason}")
        await self.futures_stream.stop()

    def now_market(self) -> datetime:
        return datetime.now(self.tz)

    def _at_market_time(self, d, t: time) -> datetime:
        return datetime.combine(d, t, tzinfo=self.tz)

    def _decision_price(self, quote: QuoteSnapshot) -> Optional[float]:
        return quote.last or quote.mid or quote.mark or quote.bid or quote.ask

    def _futures_trigger_operator(self, signal: ParsedSignal) -> str:
        return ">=" if signal.contract.option_type.value == "CALL" else "<="

    def _futures_trigger_hit(self, signal: ParsedSignal, current_price: float, trigger_price: float) -> bool:
        if signal.contract.option_type.value == "CALL":
            return current_price >= trigger_price
        return current_price <= trigger_price

    async def _async_input(self, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: input(prompt))

    def _persist_runtime(self, runtime: TradeRuntime) -> None:
        out = {
            "trade_id": runtime.trade_id,
            "status": runtime.status.value,
            "contract_symbol": runtime.contract.symbol,
            "mode": runtime.mode_selected.value,
            "quantity_original": runtime.quantity_original,
            "quantity_open": runtime.quantity_open,
            "entry_debit": runtime.entry_debit,
            "active_stop_price": runtime.active_stop_price,
            "t1_fill_credit": runtime.t1_fill_credit,
            "t2_target_price": runtime.t2_target_price,
            "t3_target_price": runtime.t3_target_price,
            "audit_log": runtime.audit_log,
        }
        path = self.config.runtime_dir / f"{runtime.trade_id}.json"
        path.write_text(json.dumps(out, indent=2, default=str))

    def _log(self, runtime: TradeRuntime, message: str) -> None:
        runtime.log(f"{self.now_market().isoformat()} {message}")
        logger.info(f"[{runtime.trade_id}] {message}")
