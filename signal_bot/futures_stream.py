from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from loguru import logger
from schwab.streaming import StreamClient

from broker_api.auth_manager import auth_manager


@dataclass(frozen=True)
class FuturesSnapshot:
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
        return self.mark or self.last


class FuturesStreamMonitor:
    def __init__(self):
        self._stream: Optional[StreamClient] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._latest: dict[str, FuturesSnapshot] = {}
        self._ready: dict[str, asyncio.Event] = {}

    async def start(self, symbols: list[str]) -> None:
        if self._stream is not None:
            return
        if auth_manager is None:
            raise RuntimeError('Schwab auth manager unavailable')
        client = auth_manager.get_client()
        stream = StreamClient(client)
        stream.add_level_one_futures_handler(self._handle_message)
        await stream.login()
        await stream.level_one_futures_subs(symbols)
        for sym in symbols:
            self._ready.setdefault(sym, asyncio.Event())
        self._stream = stream
        self._reader_task = asyncio.create_task(self._reader_loop())
        logger.info(f'Futures stream started for {symbols}')

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._stream:
            try:
                await self._stream.logout()
            except Exception as exc:
                logger.warning(f'Futures stream logout failed: {exc}')
            self._stream = None
        logger.info('Futures stream stopped')

    async def get_latest(self, symbol: str, timeout: float = 5.0) -> Optional[FuturesSnapshot]:
        if symbol in self._latest:
            return self._latest[symbol]
        event = self._ready.setdefault(symbol, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return self._latest.get(symbol)
        return self._latest.get(symbol)

    async def _reader_loop(self) -> None:
        assert self._stream is not None
        while True:
            await self._stream.handle_message()

    def _handle_message(self, msg: dict) -> None:
        for row in msg.get('content', []):
            symbol = row.get('key') or row.get('SYMBOL') or row.get('PRODUCT')
            if not symbol:
                continue
            snap = FuturesSnapshot(
                symbol=symbol,
                bid=_to_float(row.get('BID_PRICE')),
                ask=_to_float(row.get('ASK_PRICE')),
                last=_to_float(row.get('LAST_PRICE')),
                mark=_to_float(row.get('MARK')),
                raw=row,
            )
            self._latest[symbol] = snap
            self._ready.setdefault(symbol, asyncio.Event()).set()


def _to_float(v):
    if v in (None, ''):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
