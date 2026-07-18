"""Unit tests — fountain manifests, packed-id ranges, striped serving."""

from __future__ import annotations

import os
import random

import pytest

from tools.network.swarmkit.fountain import (
    DEFAULT_N_STRIPES,
    Decoder,
    FountainError,
    FountainStore,
    build_fountain_manifest,
    check_fountain_manifest,
    check_ranges,
    fountain_id,
    ids_to_ranges,
    in_ranges,
    packet_id,
    packet_length,
    roster_stripe,
    source_symbols,
)

T = 256  # small symbols keep unit tests fast


def make_object(n_symbols=40, tail=99):
    return os.urandom(T * (n_symbols - 1) + tail)


class TestManifest:
    def test_roundtrip_and_id_stability(self):
        data = make_object()
        m = build_fountain_manifest(data, T)
        assert check_fountain_manifest(m) is m
        assert m["size"] == len(data)
        assert source_symbols(m) == 40
        assert packet_length(m) == 4 + T
        assert fountain_id(m) == fountain_id(dict(m))

    def test_rejects_bad_shapes(self):
        data = make_object(4)
        good = build_fountain_manifest(data, T)
        for mutate in (
            lambda m: m.pop("object"),
            lambda m: m.update(v=2),
            lambda m: m.update(kind="blocks"),
            lambda m: m.update(algo="md5"),
            lambda m: m.update(size=0),
            lambda m: m.update(size="4"),
            lambda m: m.update(symbol_size=0),
            lambda m: m.update(symbol_size=1 << 20),
            lambda m: m.update(object="XY" * 32),
            lambda m: m.update(extra=1),
        ):
            m = dict(good)
            mutate(m)
            with pytest.raises(FountainError):
                check_fountain_manifest(m)

    def test_empty_object_refused(self):
        with pytest.raises(FountainError):
            build_fountain_manifest(b"", T)


class TestRanges:
    def test_ids_to_ranges_compresses_runs(self):
        assert ids_to_ranges([]) == []
        assert ids_to_ranges([5]) == [[5, 6]]
        assert ids_to_ranges([1, 2, 3, 7, 9, 10]) == [[1, 4], [7, 8], [9, 11]]
        # duplicates collapse
        assert ids_to_ranges([4, 4, 5]) == [[4, 6]]

    def test_membership(self):
        ranges = check_ranges([[1, 4], [7, 8]])
        inside = {1, 2, 3, 7}
        for x in range(10):
            assert in_ranges(ranges, x) == (x in inside)

    def test_check_ranges_rejects_junk(self):
        for bad in (
            "nope", [[1]], [[2, 1]], [[-1, 3]], [[0, 1 << 33]],
            [[1, True]], [[4, 6], [2, 3]], [[1, 4], [3, 6]], [["a", 2]],
        ):
            with pytest.raises(FountainError):
                check_ranges(bad)

    def test_wire_roundtrip(self):
        ids = random.Random(5).sample(range(10_000), 500)
        ranges = check_ranges(ids_to_ranges(ids))
        for i in ids:
            assert in_ranges(ranges, i)
        assert not in_ranges(ranges, 10_001)


class TestRosterStripe:
    def test_stable_and_collision_free_up_to_n(self):
        roster = [f"{i:02x}" * 32 for i in range(6)]
        stripes = [roster_stripe(p, roster) for p in roster]
        assert stripes == sorted(stripes)          # roster order
        assert len(set(stripes)) == 6              # collision-free ≤ N
        # every member computes the same assignment from any roster copy
        assert roster_stripe(roster[3], list(reversed(roster))) == stripes[3]

    def test_recycles_past_n(self):
        roster = [f"{i:02x}" * 32 for i in range(DEFAULT_N_STRIPES + 1)]
        stripes = [roster_stripe(p, roster) for p in roster]
        assert stripes[DEFAULT_N_STRIPES] == stripes[0]


class TestStore:
    def test_complete_serving_is_monotonic_and_deduped(self):
        data = make_object()
        store = FountainStore(stripe=0, n_stripes=1)
        aid = store.add_object(data, T)
        a = store.serve(aid, 10, [])
        b = store.serve(aid, 10, [])
        ids_a = {packet_id(p) for p in a}
        ids_b = {packet_id(p) for p in b}
        assert len(ids_a) == len(ids_b) == 10
        assert not ids_a & ids_b, "cursor re-served a symbol"

    def test_stripes_are_disjoint_across_seeders(self):
        data = make_object()
        stores = [FountainStore(stripe=s, n_stripes=4) for s in range(4)]
        aids = {s.add_object(data, T) for s in stores}
        (aid,) = aids  # same object -> same artifact id everywhere
        served = [
            {packet_id(p) for p in s.serve(aid, 30, [])} for s in stores
        ]
        for i in range(4):
            for j in range(i + 1, 4):
                assert not served[i] & served[j], (i, j)

    def test_same_stripe_seeders_overlap_but_still_decode(self):
        """Past n_stripes seeders, stripes recycle: duplicate waste,
        never wrong bytes — the graceful-degradation bound."""
        data = make_object()
        m = build_fountain_manifest(data, T)
        s1 = FountainStore(stripe=1, n_stripes=4)
        s2 = FountainStore(stripe=1, n_stripes=4)
        aid = s1.add_object(data, T)
        s2.add_object(data, T)
        a = {packet_id(p): p for p in s1.serve(aid, 45, [])}
        b = {packet_id(p): p for p in s2.serve(aid, 45, [])}
        assert set(a) == set(b), "same stripe emits the same positions"
        dec = Decoder.with_defaults(m["size"], T)
        result = None
        for p in {**a, **b}.values():  # dedup, as add_packet would
            result = dec.decode(p)
            if result is not None:
                break
        assert result is not None and bytes(result) == data

    def test_exclude_ranges_never_cross_the_wire(self):
        data = make_object()
        store = FountainStore(stripe=0, n_stripes=1)
        aid = store.add_object(data, T)
        first = store.serve(aid, 15, [])
        exclude = check_ranges(ids_to_ranges(packet_id(p) for p in first))
        again = store.serve(aid, 15, exclude)
        assert not {packet_id(p) for p in again} & {packet_id(p) for p in first}

    def test_partial_holder_rotor_and_exclude(self):
        data = make_object()
        seeder = FountainStore(stripe=0, n_stripes=1)
        aid = seeder.add_object(data, T)
        m = seeder.manifest(aid)

        leecher = FountainStore()
        leecher.add_manifest(aid, m)
        assert leecher.serve(aid, 5, []) == []      # nothing yet, not an error
        stash = seeder.serve(aid, 12, [])
        for p in stash:
            assert leecher.add_packet(aid, p)
            assert not leecher.add_packet(aid, p)   # duplicate detected
        held = {packet_id(p) for p in stash}
        assert set(leecher.held_ids(aid)) == held

        # rotor: successive small serves sample different packets
        r1 = {packet_id(p) for p in leecher.serve(aid, 4, [])}
        r2 = {packet_id(p) for p in leecher.serve(aid, 4, [])}
        assert r1.isdisjoint(r2)
        # exclude everything -> nothing served
        exclude = check_ranges(leecher.held_ranges(aid))
        assert leecher.serve(aid, 4, exclude) == []

    def test_manifest_verification_and_packet_length(self):
        data = make_object(6)
        seeder = FountainStore()
        aid = seeder.add_object(data, T)
        m = seeder.manifest(aid)

        other = FountainStore()
        forged = dict(m, object="0" * 64)
        with pytest.raises(FountainError):
            other.add_manifest(aid, forged)
        other.add_manifest(aid, m)
        with pytest.raises(FountainError):
            other.add_packet(aid, b"\0" * 7)        # wrong length
        with pytest.raises(FountainError):
            other.add_packet("f" * 64, b"\0" * (4 + T))  # unknown artifact

    def test_promote_requires_matching_bytes(self):
        data = make_object(6)
        seeder = FountainStore()
        aid = seeder.add_object(data, T)
        leecher = FountainStore()
        leecher.add_manifest(aid, seeder.manifest(aid))
        with pytest.raises(FountainError):
            leecher.promote(aid, b"wrong bytes")
        leecher.promote(aid, data)
        assert leecher.is_complete(aid)
        assert leecher.object(aid) == data

    def test_decode_needs_only_k_plus_epsilon_from_any_mix(self):
        """Symbols are fungible: any sufficiently large subset, from any
        stripes, in any order, reconstructs — the rateless property the
        whole transfer rests on."""
        data = make_object(50, tail=17)
        m = build_fountain_manifest(data, T)
        stores = [FountainStore(stripe=s, n_stripes=3) for s in range(3)]
        aid = stores[0].add_object(data, T)
        for s in stores[1:]:
            s.add_object(data, T)
        pool = []
        for s in stores:
            pool.extend(s.serve(aid, 40, []))
        rng = random.Random(11)
        rng.shuffle(pool)
        dec = Decoder.with_defaults(m["size"], T)
        result = None
        used = 0
        for p in pool:
            used += 1
            result = dec.decode(p)
            if result is not None:
                break
        assert result is not None, "pool did not decode"
        assert bytes(result) == data
        assert used <= source_symbols(m) + 5


class TestStoreLimits:
    def test_stripe_bounds(self):
        with pytest.raises(FountainError):
            FountainStore(stripe=8, n_stripes=8)
        with pytest.raises(FountainError):
            FountainStore(stripe=-1, n_stripes=8)
