"""Async token-bucket rate limiter for Schwab API calls."""

import asyncio
import time

from loguru import logger


class RateLimiter:
    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.tokens = float(max_calls)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        logger.info(f"RateLimiter initialized: {max_calls} calls per {period}s")

    async def acquire(self, tokens: int = 1) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_refill
                self.tokens = min(self.max_calls, self.tokens + (elapsed * self.max_calls / self.period))
                self.last_refill = now

                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return

                wait_time = (tokens - self.tokens) * self.period / self.max_calls
                logger.debug(f"Rate limit reached. Waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)

    def get_available_tokens(self) -> float:
        now = time.monotonic()
        elapsed = now - self.last_refill
        return min(self.max_calls, self.tokens + (elapsed * self.max_calls / self.period))
