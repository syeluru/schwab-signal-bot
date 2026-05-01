from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Optional

from dotenv import load_dotenv

from broker_api.auth_manager import auth_manager
from broker_api.schwab_client import schwab_client
from signal_bot.futures_stream import FuturesStreamMonitor
from signal_bot.models import OptionType
from signal_bot.schwab_adapter import SchwabSignalBotAdapter


@dataclass
class PreflightReport:
    token_path: str
    token_valid_check: bool
    account_number_configured: bool
    account_type: Optional[str]
    liquidation_value: Optional[float]
    option_buying_power: Optional[float]
    positions_count: Optional[int]
    spy_quote: dict
    es_stream_quote: dict
    resolved_option: dict


async def run_preflight(expiry: date, strike: float, option_type: OptionType) -> PreflightReport:
    load_dotenv('.env')
    adapter = SchwabSignalBotAdapter(schwab_client)

    account = await schwab_client.get_account(include_positions=True)
    sec = account.get('securitiesAccount', {})
    balances = sec.get('currentBalances', {})
    positions = sec.get('positions', [])

    spy = await adapter.get_quote('SPY')

    futures = FuturesStreamMonitor()
    await futures.start(['/ES'])
    try:
        es = await futures.get_latest('/ES', timeout=8.0)
    finally:
        await futures.stop()

    resolved = await adapter.resolve_option_contract('SPY', expiry, strike, option_type)

    return PreflightReport(
        token_path=str(getattr(auth_manager, 'token_path', '')),
        token_valid_check=auth_manager.is_token_valid() if auth_manager else False,
        account_number_configured=bool(getattr(schwab_client, 'account_id', '')),
        account_type=sec.get('type'),
        liquidation_value=_to_float(balances.get('liquidationValue')),
        option_buying_power=_to_float(balances.get('availableFundsNonMarginableTrade')),
        positions_count=len(positions),
        spy_quote={'bid': spy.bid, 'ask': spy.ask, 'last': spy.last, 'mark': spy.mark, 'mid': spy.mid},
        es_stream_quote={} if es is None else {'bid': es.bid, 'ask': es.ask, 'last': es.last, 'mark': es.mark, 'mid': es.mid, 'symbol': es.symbol},
        resolved_option={
            'symbol': resolved.symbol,
            'description': resolved.description,
            'bid': resolved.bid,
            'ask': resolved.ask,
            'last': resolved.last,
            'mark': resolved.mark,
            'mid': resolved.mid,
        },
    )


def _to_float(v):
    try:
        return None if v is None else float(v)
    except Exception:
        return None


async def _main():
    report = await run_preflight(date(2026, 5, 1), 714.0, OptionType.CALL)
    print(json.dumps(asdict(report), indent=2))


if __name__ == '__main__':
    asyncio.run(_main())
