from __future__ import annotations

from pathlib import Path

from datetime import date, datetime
from zoneinfo import ZoneInfo

from signal_bot.bot import SignalBotConfig, SignalDrivenOptionBot
from signal_bot.asset_mapping import AssetMappingError, validate_underlying_trigger_pair
from signal_bot.models import EntryMode, OptionType, ResolvedOptionContract, TradeRuntime, TradeStatus
from signal_bot.parser import SignalParser
from signal_bot.schwab_adapter import QuoteSnapshot


RAW_SIGNAL = """
9:42 AM SPY Apr 20 707C
watch first push
Agro /ES 7138
Modrate /ES 7135.25
Stop Loss 25%
T1 ScaleOut /ES 7148
leave runners if trend stays clean
""".strip()


def test_signal_parser_handles_typos_and_notes():
    parser = SignalParser(today=date(2026, 4, 19))
    parsed = parser.parse(RAW_SIGNAL)

    assert parsed.contract.underlying == "SPY"
    assert parsed.contract.expiry == date(2026, 4, 20)
    assert parsed.contract.strike == 707.0
    assert parsed.contract.option_type == OptionType.CALL
    assert parsed.entry_modes[EntryMode.AGGRO].trigger_symbol == "/ES"
    assert parsed.entry_modes[EntryMode.AGGRO].trigger_price == 7138.0
    assert parsed.entry_modes[EntryMode.MODERATE].trigger_price == 7135.25
    assert parsed.t1_rule.trigger_price == 7148.0
    assert parsed.risk_rules.initial_stop_pct == 0.25
    assert parsed.notes == ["watch first push", "leave runners if trend stays clean"]


def test_signal_parser_allows_aggro_only_and_defaults_stop_loss():
    raw = """
9:40 QQQ 28Apr 658 Put
Aggro /NQ 27166

2m "Switch" Minor High

T1 ScaleOut /NQ 27101.50

HPMR 27253.25
""".strip()
    parsed = SignalParser(today=date(2026, 4, 28)).parse(raw)

    assert parsed.signal_timestamp == datetime(2026, 4, 28, 9, 40)
    assert parsed.contract.underlying == "QQQ"
    assert parsed.contract.expiry == date(2026, 4, 28)
    assert parsed.contract.strike == 658.0
    assert parsed.contract.option_type == OptionType.PUT
    assert parsed.entry_modes[EntryMode.AGGRO].trigger_symbol == "/NQ"
    assert parsed.entry_modes[EntryMode.AGGRO].trigger_price == 27166.0
    assert EntryMode.MODERATE not in parsed.entry_modes
    assert parsed.t1_rule.trigger_price == 27101.5
    assert parsed.risk_rules.initial_stop_pct == 0.25
    assert parsed.notes == ['2m "Switch" Minor High', "HPMR 27253.25"]


def test_asset_mapping_accepts_expected_pairs():
    ok = validate_underlying_trigger_pair('SPY', '/ES')
    assert ok.trigger_symbol == '/ES'
    ok = validate_underlying_trigger_pair('QQQ', '/NQ')
    assert ok.trigger_symbol == '/NQ'


def test_asset_mapping_rejects_mismatched_pairs():
    try:
        validate_underlying_trigger_pair('QQQ', '/ES')
    except AssetMappingError as exc:
        assert 'QQQ' in str(exc)
        assert '/NQ' in str(exc)
    else:
        raise AssertionError('Expected AssetMappingError')


class FakeBroker:
    def __init__(self):
        self.sell_calls = []
        self.fills = {
            "t1": {"status": "FILLED", "avg_fill_price": 5.6},
            "t2": {"status": "FILLED", "avg_fill_price": 8.0},
        }

    async def get_quote(self, symbol: str) -> QuoteSnapshot:
        return QuoteSnapshot(symbol=symbol, bid=6.0, ask=6.2, last=6.1, mark=6.1, raw={})

    async def place_option_sell_limit(self, option_symbol: str, quantity: int, limit_price: float) -> dict:
        order_id = "t1" if not self.sell_calls else "t2"
        self.sell_calls.append((option_symbol, quantity, limit_price, order_id))
        return {"orderId": order_id}

    async def wait_for_fill(self, order_id: str, timeout_seconds: int = 120) -> dict:
        return self.fills[order_id]


class TestBot(SignalDrivenOptionBot):
    def __init__(self, broker):
        super().__init__(broker=broker, config=SignalBotConfig(runtime_dir=Path("runtime/test_signal_bot"), logs_dir=Path("logs/test_signal_bot")))
        self._now = datetime(2026, 4, 20, 10, 0, tzinfo=ZoneInfo("America/New_York"))

    def now_market(self):
        return self._now


def test_trailing_stop_updates_after_t1_and_t2():
    parsed = SignalParser(today=date(2026, 4, 19)).parse(RAW_SIGNAL)
    runtime = TradeRuntime(
        trade_id="test",
        signal=parsed,
        mode_selected=EntryMode.AGGRO,
        contract=ResolvedOptionContract(
            symbol="SPY  260420C00707000",
            description="SPY Apr 20 2026 707 Call",
            underlying="SPY",
            expiry=date(2026, 4, 20),
            strike=707.0,
            option_type=OptionType.CALL,
        ),
        quantity_original=4,
        quantity_open=4,
        status=TradeStatus.OPEN_FULL,
        entry_debit=4.0,
        active_stop_price=3.0,
        initial_stop_price=3.0,
        t2_target_price=8.0,
        t3_target_price=12.0,
    )

    broker = FakeBroker()
    bot = TestBot(broker)

    import asyncio
    asyncio.run(bot._execute_t1(runtime))
    assert runtime.status == TradeStatus.OPEN_AFTER_T1
    assert runtime.quantity_open == 2
    assert runtime.t1_fill_credit == 5.6
    assert runtime.active_stop_price == 4.1

    asyncio.run(bot._execute_t2(runtime))
    assert runtime.status == TradeStatus.OPEN_AFTER_T2
    assert runtime.quantity_open == 1
    assert runtime.active_stop_price == 5.6


def test_bot_rejects_mismatched_signal_before_broker_calls():
    raw = """
9:42 AM QQQ Apr 20 490C
Agro /ES 7138
Moderate /ES 7135.25
Stop Loss 25%
T1 ScaleOut /ES 7148
""".strip()

    class ResolveFailBroker:
        async def resolve_option_contract(self, *args, **kwargs):
            raise AssertionError('broker should not be called for incompatible mapping')

    bot = SignalDrivenOptionBot(broker=ResolveFailBroker(), config=SignalBotConfig(runtime_dir=Path('runtime/test_signal_bot'), logs_dir=Path('logs/test_signal_bot')))
    import asyncio
    try:
        asyncio.run(bot.run_signal(raw, quantity=4, mode=EntryMode.AGGRO))
    except AssetMappingError:
        pass
    else:
        raise AssertionError('Expected AssetMappingError')


UPDATE_SIGNAL = """
9:42 SPY 20Apr 707 Call
Very Aggro (Pre Market Pingala) 7141.25-7140.75

Aggro /ES 7138

Moderate /ES 7135.25

StopLoss -25%

T1 ScaleOut /ES 7148

Valid only in an UPtrend
9:49 /GC Aggro 4832.5. T1 ScaleOut /GC 4842
10:00-10:05 Volatility Window
**UPDATE** 9:42 SPY 20Apr 707 Call
Aggro /ES 7144.25

T1 ScaleOut /ES 7153

StopLoss -25%

Primary Volatility Windows
10:00-10:05 ET
10:25-10:35 ET
11:00-11:05 ET
""".strip()


def test_parser_merges_update_and_ignores_unrelated_cross_asset_lines():
    parser = SignalParser(today=date(2026, 4, 19))
    parsed = parser.parse(UPDATE_SIGNAL)

    assert parsed.contract.underlying == 'SPY'
    assert parsed.contract.expiry == date(2026, 4, 20)
    assert parsed.contract.strike == 707.0
    assert parsed.entry_modes[EntryMode.AGGRO].trigger_symbol == '/ES'
    assert parsed.entry_modes[EntryMode.AGGRO].trigger_price == 7144.25
    assert parsed.entry_modes[EntryMode.MODERATE].trigger_symbol == '/ES'
    assert parsed.entry_modes[EntryMode.MODERATE].trigger_price == 7135.25
    assert parsed.t1_rule.trigger_symbol == '/ES'
    assert parsed.t1_rule.trigger_price == 7153.0
    assert parsed.risk_rules.initial_stop_pct == 0.25
    assert parsed.signal_timestamp == datetime(2026, 4, 20, 9, 42)
    assert not any('/GC Aggro' in note for note in parsed.notes)
    assert 'Valid only in an UPtrend' in parsed.notes
    assert 'Primary Volatility Windows' in parsed.notes
    assert '10:25-10:35 ET' in parsed.notes
