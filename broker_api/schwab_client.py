"""Async Schwab client wrapper used by the signal bot."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from functools import partial
from typing import Any, Optional

from loguru import logger

try:
    from schwab.client import Client as SchwabAPIClient
    SCHWAB_AVAILABLE = True
except ImportError:  # pragma: no cover
    SchwabAPIClient = None
    SCHWAB_AVAILABLE = False
    logger.warning("schwab-py not installed. Install with: pip install schwab-py")

from broker_api.auth_manager import auth_manager
from broker_api.rate_limiter import RateLimiter
from config.settings import settings


class SchwabClient:
    def __init__(self):
        if not SCHWAB_AVAILABLE:
            raise ImportError("schwab-py is required. Install with: pip install schwab-py")
        self._client: Optional[SchwabAPIClient] = None
        self.rate_limiter = RateLimiter(settings.SCHWAB_RATE_LIMIT_CALLS, settings.SCHWAB_RATE_LIMIT_PERIOD)
        self.account_id = settings.SCHWAB_ACCOUNT_ID
        self._account_hash_cache: Optional[str] = None
        logger.info("SchwabClient initialized")

    def _get_client(self) -> SchwabAPIClient:
        if auth_manager is None:
            raise RuntimeError("Schwab auth manager unavailable. Configure SCHWAB_API_KEY and SCHWAB_API_SECRET.")
        if self._client is None:
            self._client = auth_manager.get_client()
            logger.info("Schwab client connection established")
        return self._client

    def re_authenticate(self) -> bool:
        if auth_manager is None:
            return False
        try:
            client = auth_manager.authenticate_interactive()
            self._client = client
            self._account_hash_cache = None
            return True
        except Exception as exc:
            logger.error(f"Re-authentication failed: {exc}")
            return False

    async def _rate_limited_call(self, func: callable, *args, **kwargs) -> Any:
        await self.rate_limiter.acquire()
        loop = asyncio.get_running_loop()
        call = partial(func, *args, **kwargs)
        return await loop.run_in_executor(None, call)

    async def get_quote(self, symbol: str) -> dict:
        response = await self._rate_limited_call(self._get_client().get_quote, symbol)
        return _json_or_raise(response, f"get quote for {symbol}")

    async def get_quotes(self, symbols: list[str]) -> dict:
        response = await self._rate_limited_call(self._get_client().get_quotes, symbols)
        return _json_or_raise(response, "get quotes")

    async def get_option_chain(
        self,
        symbol: str,
        contract_type: Optional[str] = None,
        strike_count: Optional[int] = None,
        include_underlying_quote: bool = True,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        strike_range: Optional[str] = None,
    ) -> dict:
        from schwab.client import Client

        if symbol.upper() in {"SPX", "VIX", "RUT", "NDX", "DJX"}:
            symbol = f"${symbol.upper()}"

        kwargs: dict[str, Any] = {}
        if contract_type:
            kwargs["contract_type"] = {
                "CALL": Client.Options.ContractType.CALL,
                "PUT": Client.Options.ContractType.PUT,
            }.get(contract_type.upper(), Client.Options.ContractType.ALL)
        if strike_count is not None:
            kwargs["strike_count"] = strike_count
        if include_underlying_quote:
            kwargs["include_underlying_quote"] = True
        if from_date is not None:
            kwargs["from_date"] = from_date
        if to_date is not None:
            kwargs["to_date"] = to_date
        if strike_range:
            kwargs["strike_range"] = {
                "ITM": Client.Options.StrikeRange.IN_THE_MONEY,
                "NTM": Client.Options.StrikeRange.NEAR_THE_MONEY,
                "OTM": Client.Options.StrikeRange.OUT_OF_THE_MONEY,
                "SAM": Client.Options.StrikeRange.STRIKES_ABOVE_MARKET,
                "SBM": Client.Options.StrikeRange.STRIKES_BELOW_MARKET,
                "SNM": Client.Options.StrikeRange.STRIKES_NEAR_MARKET,
                "ALL": Client.Options.StrikeRange.ALL,
            }.get(strike_range.upper())

        response = await self._rate_limited_call(self._get_client().get_option_chain, symbol, **kwargs)
        return _json_or_raise(response, f"get option chain for {symbol}")

    async def _get_account_hash(self) -> str:
        if self._account_hash_cache:
            return self._account_hash_cache

        response = await self._rate_limited_call(self._get_client().get_account_numbers)
        accounts = _json_or_raise(response, "get account numbers")
        if not accounts or not isinstance(accounts, list):
            raise ValueError("No Schwab accounts found")

        if self.account_id:
            for account in accounts:
                if account.get("accountNumber") == self.account_id:
                    hash_value = account.get("hashValue")
                    if not hash_value:
                        raise ValueError("No hash value for configured account")
                    self._account_hash_cache = hash_value
                    logger.info("Found configured account hash")
                    return hash_value
            logger.warning("Configured Schwab account was not found; falling back to first available account")

        hash_value = accounts[0].get("hashValue")
        if not hash_value:
            raise ValueError("No hash value in account data")
        self._account_hash_cache = hash_value
        logger.info("Using first available Schwab account")
        return hash_value

    async def get_account(self, account_id: Optional[str] = None, include_positions: bool = True) -> dict:
        client = self._get_client()
        kwargs = {}
        if include_positions:
            kwargs["fields"] = [client.Account.Fields.POSITIONS]
        response = await self._rate_limited_call(client.get_account, account_id or await self._get_account_hash(), **kwargs)
        return _json_or_raise(response, "get account")

    async def get_account_positions(self, account_id: Optional[str] = None) -> list[dict]:
        account_data = await self.get_account(account_id)
        return account_data.get("securitiesAccount", {}).get("positions", [])

    async def get_buying_power(self, account_id: Optional[str] = None) -> float:
        account_data = await self.get_account(account_id)
        balances = account_data.get("securitiesAccount", {}).get("currentBalances", {})
        option_bp = float(balances.get("availableFundsNonMarginableTrade", 0.0) or 0.0)
        equity_bp = float(balances.get("buyingPower", 0.0) or 0.0)
        return option_bp if option_bp > 0 else equity_bp

    async def place_order(self, order: dict, account_id: Optional[str] = None) -> dict:
        if settings.PAPER_TRADING:
            logger.warning("Paper trading mode - order not actually placed")
            return {"status": "PAPER_TRADING", "order": order, "message": "Set PAPER_TRADING=False for live orders"}

        response = await self._rate_limited_call(self._get_client().place_order, account_id or await self._get_account_hash(), order)
        if response.status_code not in (200, 201):
            raise ValueError(f"Failed to place order: API error {response.status_code}: {getattr(response, 'text', '')}")

        result = response.json() if getattr(response, "text", "") else {}
        location = response.headers.get("Location", "")
        if location:
            result["orderId"] = location.rstrip("/").split("/")[-1]
        return result or {"status": "success"}

    async def get_order(self, order_id: str, account_id: Optional[str] = None) -> dict:
        response = await self._rate_limited_call(self._get_client().get_order, order_id, account_id or await self._get_account_hash())
        return _json_or_raise(response, f"get order {order_id}")

    async def get_orders_for_account(
        self,
        account_id: Optional[str] = None,
        status: Optional[str] = None,
        from_entered_datetime: Optional[datetime] = None,
        to_entered_datetime: Optional[datetime] = None,
        max_results: Optional[int] = None,
    ) -> list[dict]:
        client = self._get_client()
        kwargs: dict[str, Any] = {}
        if status:
            kwargs["status"] = client.Order.Status[status]
        if from_entered_datetime:
            kwargs["from_entered_datetime"] = from_entered_datetime
        if to_entered_datetime:
            kwargs["to_entered_datetime"] = to_entered_datetime
        if max_results:
            kwargs["max_results"] = max_results
        response = await self._rate_limited_call(client.get_orders_for_account, account_id or await self._get_account_hash(), **kwargs)
        return _json_or_raise(response, "get account orders")

    async def cancel_order(self, order_id: str, account_id: Optional[str] = None) -> bool:
        if settings.PAPER_TRADING:
            logger.warning("Paper trading mode - cancel simulation")
            return True
        response = await self._rate_limited_call(self._get_client().cancel_order, order_id, account_id or await self._get_account_hash())
        return response.status_code in (200, 204)


def _json_or_raise(response, action: str):
    if response.status_code != 200:
        raise ValueError(f"Failed to {action}: API error {response.status_code}: {getattr(response, 'text', '')}")
    return response.json()


schwab_client = SchwabClient()
