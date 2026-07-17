"""Store + manifest: content addressing is the integrity story."""

from __future__ import annotations

import os

import pytest

from tools.network.swarmkit.store import (
    BlockError,
    BlockStore,
    bitmap_hex_to_indices,
    build_manifest,
    check_manifest,
    indices_to_bitmap_hex,
    manifest_id,
)

BLOCK = 4096


def artifact(size=BLOCK * 5 + 123):
    return os.urandom(size)


class TestManifest:
    def test_roundtrip_and_id_stability(self):
        data = artifact()
        m1 = build_manifest(data, BLOCK)
        m2 = build_manifest(data, BLOCK)
        assert m1 == m2
        assert manifest_id(m1) == manifest_id(m2)
        assert len(m1["blocks"]) == 6  # 5 full + 1 short
        check_manifest(m1)

    def test_different_data_different_id(self):
        a, b = artifact(), artifact()
        assert manifest_id(build_manifest(a, BLOCK)) != manifest_id(
            build_manifest(b, BLOCK)
        )

    @pytest.mark.parametrize("mangle", [
        lambda m: m.pop("size"),
        lambda m: m.update(v=99),
        lambda m: m.update(algo="md5"),
        lambda m: m.update(size=-1),
        lambda m: m.update(blocks=[]),
        lambda m: m["blocks"].append("ff" * 32),        # count vs size
        lambda m: m["blocks"].__setitem__(0, "junk"),
        lambda m: m["blocks"].__setitem__(0, "FF" * 32),  # uppercase
        lambda m: m.update(extra=1),
    ])
    def test_malformed_manifests_rejected(self, mangle):
        m = build_manifest(artifact(), BLOCK)
        mangle(m)
        with pytest.raises(BlockError):
            check_manifest(m)

    def test_empty_artifact_rejected(self):
        with pytest.raises(BlockError):
            build_manifest(b"", BLOCK)


class TestBlockStore:
    def test_seed_then_assemble(self):
        data = artifact()
        store = BlockStore()
        aid = store.add_artifact(data, BLOCK)
        assert store.is_complete(aid)
        assert store.assemble(aid) == data
        assert store.have(aid) == set(range(6))

    def test_fetcher_path_verifies_every_block(self):
        data = artifact()
        seed = BlockStore()
        aid = seed.add_artifact(data, BLOCK)

        store = BlockStore()
        store.add_manifest(aid, seed.manifest(aid))
        for i in range(6):
            assert store.add_block(aid, i, seed.get_block(aid, i))
        assert store.assemble(aid) == data

    def test_corrupt_block_refused(self):
        data = artifact()
        seed = BlockStore()
        aid = seed.add_artifact(data, BLOCK)
        store = BlockStore()
        store.add_manifest(aid, seed.manifest(aid))

        good = seed.get_block(aid, 2)
        bad = good[:-1] + bytes([good[-1] ^ 0xFF])
        assert not store.add_block(aid, 2, bad)
        assert not store.add_block(aid, 2, good + b"x")   # wrong length
        assert not store.add_block(aid, 1, good)          # right bytes, wrong slot
        assert 2 not in store.have(aid)
        assert store.add_block(aid, 2, good)

    def test_forged_manifest_refused(self):
        data = artifact()
        seed = BlockStore()
        aid = seed.add_artifact(data, BLOCK)
        forged = dict(seed.manifest(aid))
        forged["blocks"] = list(forged["blocks"])
        forged["blocks"][0] = "ab" * 32
        store = BlockStore()
        with pytest.raises(BlockError):
            store.add_manifest(aid, forged)

    def test_unknown_artifact_and_bad_index(self):
        store = BlockStore()
        with pytest.raises(BlockError):
            store.add_block("00" * 32, 0, b"x")
        data = artifact()
        aid = store.add_artifact(data, BLOCK)
        with pytest.raises(BlockError):
            store.add_block(aid, 99, b"x")
        with pytest.raises(BlockError):
            BlockStore().assemble(aid)


class TestBitmap:
    def test_roundtrip(self):
        total = 21
        for indices in (set(), {0}, {20}, {0, 7, 8, 15, 16, 20}, set(range(total))):
            bitmap = indices_to_bitmap_hex(indices, total)
            assert len(bitmap) == 2 * ((total + 7) // 8)
            assert bitmap_hex_to_indices(bitmap, total) == indices

    def test_junk_rejected(self):
        for junk in ("zz", "ff", 42, "ffffffffffff"):
            with pytest.raises(BlockError):
                bitmap_hex_to_indices(junk, 21)
