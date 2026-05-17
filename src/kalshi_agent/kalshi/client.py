"""Kalshi REST client with RSA-PSS auth, rate limiting, retry, idempotency."""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_agent.journal.logger import get_logger
from kalshi_agent.kalshi.types import (
    KalshiBalance,
    KalshiFill,
    KalshiPosition,
    Market,
    OrderRequest,
    OrderResponse,
)

log = get_logger(__name__)


class KalshiAuthError(Exception):
    pass


class KalshiRateLimitError(Exception):
    pass


class KalshiAPIError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Kalshi API {status_code}: {body[:200]}")


class TokenBucket:
    """Async token bucket — refills at `rate` tokens/sec, capacity == rate."""

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._capacity = max(rate, 1.0)
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


def _load_private_key(path: Path):
    data = path.read_bytes()
    return serialization.load_pem_private_key(data, password=None)


def sign_request(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """Sign per Kalshi spec: base64(RSA-PSS-SHA256(timestamp + METHOD + path_without_query)).

    The path must be the raw API path WITHOUT query string, e.g. `/trade-api/v2/portfolio/orders`.
    """
    message = (timestamp_ms + method.upper() + path).encode("utf-8")
    sig = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii")


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        api_key_id: str,
        private_key_path: Path,
        rate_limit_reads_per_second: float = 18.0,
        rate_limit_writes_per_second: float = 8.0,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key_id = api_key_id
        self._private_key = _load_private_key(private_key_path)
        self._read_bucket = TokenBucket(rate_limit_reads_per_second)
        self._write_bucket = TokenBucket(rate_limit_writes_per_second)
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> KalshiClient:
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self, method: str, path_no_query: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        sig = sign_request(self._private_key, ts, method, path_no_query)
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        max_retries: int = 3,
    ) -> dict:
        assert self._client is not None, "use as async context manager"
        bucket = self._write_bucket if method.upper() in {"POST", "DELETE", "PUT"} else self._read_bucket

        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            await bucket.acquire()
            headers = self._headers(method, _api_path(path))
            try:
                resp = await self._client.request(
                    method, url, params=params, json=json, headers=headers
                )
            except httpx.HTTPError as e:
                last_exc = e
                await asyncio.sleep(2**attempt)
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", 1.0))
                await asyncio.sleep(retry_after + 0.5)
                continue
            if 500 <= resp.status_code < 600:
                await asyncio.sleep(2**attempt)
                last_exc = KalshiAPIError(resp.status_code, resp.text)
                continue
            if resp.status_code == 401 or resp.status_code == 403:
                raise KalshiAuthError(f"{resp.status_code}: {resp.text[:200]}")
            if resp.status_code >= 400:
                raise KalshiAPIError(resp.status_code, resp.text)
            return resp.json() if resp.content else {}

        if last_exc:
            raise last_exc
        raise KalshiAPIError(0, "exhausted retries")

    # --- Endpoints -----------------------------------------------------------

    async def get_market(self, ticker: str) -> Market:
        data = await self._request("GET", f"/markets/{ticker}")
        return Market.model_validate(data.get("market", data))

    async def list_positions(self) -> list[KalshiPosition]:
        data = await self._request("GET", "/portfolio/positions")
        return [KalshiPosition.model_validate(p) for p in data.get("market_positions", [])]

    async def list_orders(self, status: str | None = None) -> list[OrderResponse]:
        params = {"status": status} if status else None
        data = await self._request("GET", "/portfolio/orders", params=params)
        return [OrderResponse.model_validate(o) for o in data.get("orders", [])]

    async def create_order(self, req: OrderRequest) -> OrderResponse:
        body = req.model_dump(exclude_none=True)
        data = await self._request("POST", "/portfolio/orders", json=body)
        order = data.get("order", data)
        return OrderResponse.model_validate({**order, "raw": data})

    async def cancel_order(self, order_id: str) -> None:
        await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def cancel_all_resting(self) -> int:
        """Cancel every open/resting order on the account. Returns number cancelled."""
        resting = await self.list_orders(status="resting")
        n = 0
        for o in resting:
            try:
                await self.cancel_order(o.order_id)
                n += 1
            except KalshiAPIError as e:
                log.warning("cancel_failed", order_id=o.order_id, error=str(e))
        return n

    async def get_order(self, order_id: str) -> OrderResponse:
        data = await self._request("GET", f"/portfolio/orders/{order_id}")
        return OrderResponse.model_validate(data.get("order", data))

    async def get_balance(self) -> KalshiBalance:
        data = await self._request("GET", "/portfolio/balance")
        return KalshiBalance.model_validate(data)

    async def list_fills(
        self,
        *,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[list[KalshiFill], str | None]:
        """List fills (executions) on the account. Returns (fills, next_cursor)."""
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        if cursor:
            params["cursor"] = cursor
        data = await self._request("GET", "/portfolio/fills", params=params)
        fills = [KalshiFill.model_validate(f) for f in data.get("fills", [])]
        return fills, data.get("cursor")


def _api_path(path: str) -> str:
    """Construct the signing path: `/trade-api/v2` prefix is part of every Kalshi path."""
    if path.startswith("/trade-api/"):
        return path.split("?", 1)[0]
    return f"/trade-api/v2{path}".split("?", 1)[0]
