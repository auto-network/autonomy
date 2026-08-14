"""Per-channel routing between ordinary application work and one ICE attempt.

The same authenticated, encrypted channel endpoint carries both capabilities,
but a connection chooses exactly one on its first application message.  This
keeps the existing artifact handler unchanged while giving the short-lived ICE
connection real teardown and receive-deadline semantics.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time

from .ice_signaling import (
    ATTEMPT_DEADLINE_SECONDS,
    ICE_POLICIES,
    IceCapacity,
    IceSignalingError,
    IceSignalingSession,
)


class IceRoutingHandler:
    """Factory for isolated per-connection application-or-ICE routers."""

    def __init__(
        self,
        application_handler,
        *,
        policy_provider,
        configuration_provider,
        responder_factory_provider,
        capacity: IceCapacity,
        first_message_timeout: float = ATTEMPT_DEADLINE_SECONDS,
    ):
        if first_message_timeout <= 0:
            raise ValueError("first-message timeout must be positive")
        self.application_handler = application_handler
        self.policy_provider = policy_provider
        self.configuration_provider = configuration_provider
        self.responder_factory_provider = responder_factory_provider
        self.capacity = capacity
        self.first_message_timeout = float(first_message_timeout)

    def for_channel(self, token: str):
        return _ChannelRouter(self, token)


class _ChannelRouter:
    def __init__(self, owner: IceRoutingHandler, token: str):
        self.owner = owner
        self.token = token
        self.mode: str | None = None
        self.delegate = None

    def receive_timeout(self):
        if self.mode is None:
            # A separately handshaken signaling socket that never sends begin
            # must not occupy a channel forever. Ordinary clients send their
            # first artifact request immediately and become unbounded afterward.
            return self.owner.first_message_timeout
        timeout = getattr(self.delegate, "receive_timeout", None)
        return None if timeout is None else timeout()

    @staticmethod
    def _operation(raw: bytes) -> str | None:
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, ValueError, RecursionError, TypeError):
            return None
        return value.get("op") if isinstance(value, dict) else None

    async def _application_delegate(self):
        delegate = self.owner.application_handler
        factory = getattr(delegate, "for_channel", None)
        if factory is not None:
            delegate = factory(self.token)
            if inspect.isawaitable(delegate):
                delegate = await delegate
        return delegate

    def _remaining_start_time(self, started_at: float) -> float:
        remaining = self.owner.first_message_timeout - (time.monotonic() - started_at)
        if remaining <= 0:
            raise IceSignalingError("ICE start deadline elapsed")
        return remaining

    async def _ice_delegate(self, started_at: float):
        policy = self.owner.policy_provider(self.token)
        if inspect.isawaitable(policy):
            try:
                policy = await asyncio.wait_for(
                    policy, timeout=self._remaining_start_time(started_at)
                )
            except asyncio.TimeoutError as exc:
                raise IceSignalingError("ICE policy resolution timed out") from exc
        if policy not in ICE_POLICIES:
            raise IceSignalingError("the verified grant has no valid ICE policy")
        responder_factory = self.owner.responder_factory_provider(self.token)
        if inspect.isawaitable(responder_factory):
            try:
                responder_factory = await asyncio.wait_for(
                    responder_factory,
                    timeout=self._remaining_start_time(started_at),
                )
            except asyncio.TimeoutError as exc:
                raise IceSignalingError("ICE responder resolution timed out") from exc
        return IceSignalingSession(
            token=self.token,
            policy=policy,
            configuration_provider=self.owner.configuration_provider,
            responder_factory=responder_factory,
            capacity=self.owner.capacity,
            deadline_seconds=self._remaining_start_time(started_at),
        )

    async def __call__(self, token: str, raw: bytes):
        if token != self.token:
            raise IceSignalingError("channel token changed after authentication")
        if self.mode is None:
            if self._operation(raw) == "ice.begin":
                started_at = time.monotonic()
                self.mode = "ice"
                self.delegate = await self._ice_delegate(started_at)
            else:
                self.mode = "application"
                self.delegate = await self._application_delegate()
        result = self.delegate(token, raw)
        if inspect.isawaitable(result):
            result = await result
        return result

    def on_response_sent(self):
        sent = getattr(self.delegate, "on_response_sent", None)
        return False if sent is None else sent()

    async def aclose(self):
        close = getattr(self.delegate, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
