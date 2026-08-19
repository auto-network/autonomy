from itertools import permutations

from tools.network.fleet_sync_sim.codec import Mutation
from tools.network.fleet_sync_sim.merge import MutationInbox


def _candidate(timestamp: int, title: str) -> Mutation:
    return Mutation(
        "sources", ("source-1",), timestamp, False, (("title", title),)
    )


def test_all_arrival_orders_converge_with_tombstones() -> None:
    candidates = [
        _candidate(10, "old"),
        _candidate(20, "concurrent-a"),
        _candidate(20, "concurrent-b"),
        Mutation("sources", ("source-1",), 30, True),
    ]
    digests = set()
    for order in permutations(candidates):
        inbox = MutationInbox()
        inbox.ingest(order)
        inbox.ingest(order)  # replay is inert
        digests.add(inbox.digest())
        assert inbox.winners() == [candidates[-1]]
    assert len(digests) == 1


def test_timestamp_then_hash_resolves_true_concurrency() -> None:
    left = _candidate(20, "a")
    right = _candidate(20, "b")
    expected = max((left, right), key=lambda mutation: mutation.candidate_hash)
    for order in ((left, right), (right, left)):
        inbox = MutationInbox()
        inbox.ingest(order)
        assert inbox.winners() == [expected]


def test_independent_addresses_are_union_not_replacement() -> None:
    local = Mutation(
        "sources", ("local",), 50, False, (("title", "local-only"),)
    )
    checkpoint = Mutation(
        "sources", ("remote",), 10, False, (("title", "checkpoint"),)
    )
    inbox = MutationInbox()
    inbox.ingest([local])
    inbox.ingest([checkpoint])
    assert {mutation.address for mutation in inbox.winners()} == {
        ("local",), ("remote",)
    }
