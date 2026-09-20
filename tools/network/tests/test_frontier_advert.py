"""The connector's frontier advert decision (auto-xs9hz): an unchanged map
sends nothing, a value never regresses, and a fresh persona is sent."""
from tools.network.fleet_relay_sync import frontier_advert

P1, P2 = "11" * 32, "22" * 32


def test_unchanged_frontier_sends_nothing():
    assert frontier_advert({P1: 10}, {P1: 10}) is None
    assert frontier_advert({P1: 10}, {}) is None


def test_a_rise_or_a_new_persona_sends_the_merged_map():
    assert frontier_advert({P1: 10}, {P1: 12}) == {P1: 12}
    assert frontier_advert({P1: 10}, {P2: 3}) == {P1: 10, P2: 3}


def test_a_value_never_regresses():
    assert frontier_advert({P1: 10, P2: 5}, {P1: 4, P2: 5}) is None
    assert frontier_advert({P1: 10}, {P1: 4, P2: 1}) == {P1: 10, P2: 1}


def test_the_loop_sends_on_a_wake_and_never_on_its_own(monkeypatch):
    """O-C: the advert follows a wake (the hello, or the dashboard's
    'advertise' op after a pull moved a persona floor); nothing polls."""
    import asyncio
    from tools.network import fleet_relay_sync as frs

    class _Conn:
        org = "org-uuid"
        def __init__(self):
            self.connected = asyncio.Event()
            self.frontier_wake = asyncio.Event()
            self.sent = []
        async def control(self, op, args):
            self.sent.append((op, args)); return {"ok": True}

    class _Store:
        frontier = {P1: 10}
        def covered_persona_frontiers(self):
            return dict(self.frontier)

    store = _Store()
    monkeypatch.setattr(frs, "_scoped_store", lambda scope, pub: store)

    async def run():
        conn = _Conn()
        task = asyncio.create_task(frs.advertise_frontiers_loop(conn, "org", "m" * 64, interval_s=0.05))
        await asyncio.sleep(0.2)
        assert conn.sent == []                       # no wake, no read, no send
        conn.connected.set(); conn.frontier_wake.set()   # the hello
        await asyncio.sleep(0.1)
        assert conn.sent == [("sync-frontier", {"org_uuid": "org-uuid", "frontiers": {P1: 10}})]
        store.frontier = {P1: 12}
        await asyncio.sleep(0.2)
        assert len(conn.sent) == 1                   # a change nobody signalled is not sent
        conn.frontier_wake.set()                     # the dashboard's advertise op
        await asyncio.sleep(0.1)
        assert conn.sent[-1] == ("sync-frontier", {"org_uuid": "org-uuid", "frontiers": {P1: 12}})
        conn.frontier_wake.set()                     # a wake with nothing new sends nothing
        await asyncio.sleep(0.1)
        assert len(conn.sent) == 2
        task.cancel()
    asyncio.run(run())
