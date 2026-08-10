"""The connector's push seam (auto-albp6.8).

``handler(token, message) -> response`` is invoked only by an inbound
record and never holds a reference to ``send``, so before this seam there
was no path for the serving side to emit anything unsolicited. Publisher
closes that gap WITHOUT touching the request/response loop: a connector
constructed with no publisher behaves exactly as it did before.

A publish is addressed by TOKEN, not by channel -- one frame leaves the
tunnel per batch regardless of audience size, and the relay's Stream
(auto-albp6.7) fans it out.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.network.relaykit.connector import Publisher
from tools.network.relaykit.frames import CHANNEL_ID_LEN, FRAME_DATA

TOKEN = "ab" * 16  # 32 hex chars == 16 raw bytes == CHANNEL_ID_LEN


class _Sender:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.frames: list[tuple] = []

    async def __call__(self, frame_type, channel_id, payload=b""):
        if self.fail:
            raise RuntimeError("tunnel died mid-publish")
        self.frames.append((frame_type, channel_id, payload))


def test_publish_addresses_by_token_and_sends_once():
    async def run():
        publisher, sender = Publisher(), _Sender()
        publisher.bind(sender)
        publisher.attached(TOKEN)
        assert await publisher.publish(TOKEN, b"sealed") is True
        assert sender.frames == [(FRAME_DATA, bytes.fromhex(TOKEN), b"sealed")]

    asyncio.run(run())


def test_one_frame_per_publish_regardless_of_listener_count():
    async def run():
        for listeners in (1, 50):
            publisher, sender = Publisher(), _Sender()
            publisher.bind(sender)
            for _ in range(listeners):
                publisher.attached(TOKEN)
            await publisher.publish(TOKEN, b"sealed")
            # THE D1 measure at the connector: outbound frames do not
            # vary with audience size.
            assert len(sender.frames) == 1

    asyncio.run(run())


def test_no_listeners_means_no_work():
    async def run():
        publisher, sender = Publisher(), _Sender()
        publisher.bind(sender)
        assert publisher.has_listeners(TOKEN) is False
        assert await publisher.publish(TOKEN, b"sealed") is False
        assert sender.frames == []

    asyncio.run(run())


def test_thousand_unwatched_tokens_produce_no_frames():
    async def run():
        publisher, sender = Publisher(), _Sender()
        publisher.bind(sender)
        for i in range(1000):
            await publisher.publish(f"{i:032x}", b"sealed")
        assert sender.frames == []

    asyncio.run(run())


def test_publish_without_a_tunnel_fails_closed():
    async def run():
        publisher = Publisher()  # never bound
        publisher.attached(TOKEN)
        assert await publisher.publish(TOKEN, b"sealed") is False

    asyncio.run(run())


def test_unbind_stops_further_publishes():
    async def run():
        publisher, sender = Publisher(), _Sender()
        publisher.bind(sender)
        publisher.attached(TOKEN)
        await publisher.publish(TOKEN, b"first")
        publisher.unbind()
        assert await publisher.publish(TOKEN, b"second") is False
        assert len(sender.frames) == 1

    asyncio.run(run())


def test_send_failure_drops_the_frame_rather_than_raising():
    async def run():
        publisher, sender = Publisher(), _Sender(fail=True)
        publisher.bind(sender)
        publisher.attached(TOKEN)
        # Best-effort by design: the viewer recovers the gap through
        # history on its own channel, so a dropped frame is not an error.
        assert await publisher.publish(TOKEN, b"sealed") is False

    asyncio.run(run())


@pytest.mark.parametrize("bad_token", ["not-hex", "ab", "ab" * 32, ""])
def test_a_token_that_is_not_a_channel_id_is_refused(bad_token):
    async def run():
        publisher, sender = Publisher(), _Sender()
        publisher.bind(sender)
        publisher._attached[bad_token] = 1  # force past the listener check
        assert await publisher.publish(bad_token, b"sealed") is False
        assert sender.frames == []

    asyncio.run(run())


def test_attach_and_detach_are_reference_counted():
    publisher = Publisher()
    publisher.attached(TOKEN)
    publisher.attached(TOKEN)
    assert publisher.has_listeners(TOKEN) is True
    publisher.detached(TOKEN)
    assert publisher.has_listeners(TOKEN) is True  # one channel still open
    publisher.detached(TOKEN)
    assert publisher.has_listeners(TOKEN) is False


def test_detaching_an_unknown_token_is_harmless():
    publisher = Publisher()
    publisher.detached("never-attached")
    assert publisher.has_listeners("never-attached") is False


def test_the_stream_key_never_reaches_the_publisher():
    """Publisher moves opaque bytes only -- it has no key parameter, no
    key attribute, and no way to seal anything. The application side
    seals before handing a frame over."""
    publisher = Publisher()
    assert not any("key" in name.lower() for name in vars(publisher))
    import inspect
    assert "key" not in inspect.signature(Publisher.publish).parameters
