"""Kalshi WebSocket client. Auth via RSA-signed headers on the WS handshake.

Channels of interest: `ticker` (live price book) and `fill` (private fills).
Connects, subscribes, dispatches to handlers; reconnects with backoff.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import websockets

from kalshi_agent.journal.logger import get_logger
from kalshi_agent.kalshi.client import _api_path, _load_private_key, sign_request

log = get_logger(__name__)


class KalshiWebSocket:
    """Long-lived WS connection. Watchdog tracks last_message_at."""

    def __init__(
        self,
        ws_url: str,
        api_key_id: str,
        private_key_path: Path,
        watchdog_timeout: float = 60.0,
    ) -> None:
        self._ws_url = ws_url
        self._api_key_id = api_key_id
        self._private_key = _load_private_key(private_key_path)
        self._watchdog_timeout = watchdog_timeout
        self._subscriptions: list[dict] = []
        self._handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._cmd_id = 0
        self._last_message_at = time.monotonic()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def seconds_since_last_message(self) -> float:
        return time.monotonic() - self._last_message_at

    def on(self, message_type: str, handler: Callable[[dict], Awaitable[None]]) -> None:
        self._handlers[message_type] = handler

    def subscribe(self, channel: str, **params) -> None:
        """Queue a subscription. Sent on every (re)connect."""
        self._subscriptions.append({"channel": channel, **params})

    def _auth_headers(self) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        # WS handshake signs the WS path (no method semantics — Kalshi uses GET)
        sig = sign_request(self._private_key, ts, "GET", _api_path("/ws/v2"))
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }

    async def _send_subscribe(self, ws) -> None:
        for sub in self._subscriptions:
            self._cmd_id += 1
            msg = {"id": self._cmd_id, "cmd": "subscribe", "params": sub}
            await ws.send(json.dumps(msg))

    async def run(self, stop_event: asyncio.Event) -> None:
        attempt = 0
        while not stop_event.is_set():
            try:
                async with websockets.connect(
                    self._ws_url,
                    additional_headers=self._auth_headers(),
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    self._connected = True
                    self._last_message_at = time.monotonic()
                    log.info("ws_connected", url=self._ws_url)
                    await self._send_subscribe(ws)
                    attempt = 0
                    async for raw in ws:
                        self._last_message_at = time.monotonic()
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("ws_bad_json", raw=raw[:200])
                            continue
                        msg_type = msg.get("type") or msg.get("channel") or "unknown"
                        handler = self._handlers.get(msg_type)
                        if handler:
                            try:
                                await handler(msg)
                            except Exception as e:
                                log.error("ws_handler_error", type=msg_type, error=str(e))
                        if stop_event.is_set():
                            break
            except Exception as e:
                self._connected = False
                attempt += 1
                backoff = min(30.0, (2 ** attempt)) + random.uniform(0, 1)
                log.warning("ws_disconnected", error=str(e), attempt=attempt,
                            backoff_seconds=backoff)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                except TimeoutError:
                    pass
        self._connected = False
        log.info("ws_stopped")


class WSWatchdog:
    """Trips the kill switch if the WS hasn't received a message in `timeout_seconds`.

    Only enforces during the configured trading window (open WS is expected to receive
    at minimum heartbeat/ping frames every few seconds).
    """

    def __init__(self, ws: KalshiWebSocket, kill_switch, timeout_seconds: float = 60.0) -> None:
        self._ws = ws
        self._kill = kill_switch
        self._timeout = timeout_seconds

    async def run(self, stop_event: asyncio.Event, tick_seconds: float = 5.0) -> None:
        # Wait for initial connect before arming
        while not self._ws.connected and not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
            except TimeoutError:
                pass
        while not stop_event.is_set():
            if self._ws.seconds_since_last_message > self._timeout and self._ws.connected:
                if not self._kill.is_engaged():
                    self._kill.engage(
                        f"ws_silent: {self._ws.seconds_since_last_message:.0f}s",
                        source="ws_watchdog",
                        payload={"silence_seconds": self._ws.seconds_since_last_message},
                    )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
            except TimeoutError:
                pass
