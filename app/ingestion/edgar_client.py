"""Shared async HTTP client for SEC EDGAR with rate limiting and retry."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger(__name__)

DATA_BASE_URL = "https://data.sec.gov"
FILINGS_BASE_URL = "https://www.sec.gov"


class _TokenBucket:
    """Token bucket — allows up to `rate` acquire() calls per second."""

    def __init__(self, rate: float = 10.0) -> None:
        self._rate = rate
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens < 1:
                await asyncio.sleep((1 - self._tokens) / self._rate)
                self._tokens = 0
            else:
                self._tokens -= 1


class EdgarClient:
    """Rate-limited async HTTP client for all SEC EDGAR requests."""

    def __init__(self) -> None:
        settings = get_settings()
        email = settings.sec_user_agent_email
        if not email or "example.com" in email:
            logger.warning("SEC_USER_AGENT_EMAIL is a placeholder — EDGAR will likely return 403")
        self._headers = {"User-Agent": f"FinAgent {email}"}
        self._bucket = _TokenBucket(rate=10.0)

    async def get(self, url: str) -> bytes:
        """Rate-limited GET with exponential-backoff retry (3 attempts)."""
        async with httpx.AsyncClient(
            headers=self._headers, timeout=60.0, follow_redirects=True
        ) as http:
            async for attempt in AsyncRetrying(
                wait=wait_exponential(multiplier=1, min=1, max=4),
                stop=stop_after_attempt(3),
                reraise=True,
            ):
                with attempt:
                    await self._bucket.acquire()
                    resp = await http.get(url)
                    if resp.status_code == 403:
                        logger.error(
                            "EDGAR returned 403 for %s — set SEC_USER_AGENT_EMAIL in .env", url
                        )
                    resp.raise_for_status()
                    logger.debug("GET %s → %d (%d bytes)", url, resp.status_code, len(resp.content))
                    return resp.content
        raise RuntimeError("unreachable")  # AsyncRetrying reraises on exhaustion


# Module-level singleton shared by all fetcher modules
edgar_client = EdgarClient()
