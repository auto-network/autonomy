from __future__ import annotations

import pytest

from tools.dashboard.services.trusted_git_object_store import (
    ContentAddressedStore,
    ObjectIntegrityError,
    sha256_hex,
)


def test_put_get_round_trips(tmp_path):
    store = ContentAddressedStore(tmp_path)
    digest = store.put(b"hello tree object bytes")
    assert digest == sha256_hex(b"hello tree object bytes")
    assert store.get(digest) == b"hello tree object bytes"


def test_put_is_idempotent_and_dedupes_to_one_file(tmp_path):
    store = ContentAddressedStore(tmp_path)
    d1 = store.put(b"same bytes")
    d2 = store.put(b"same bytes")
    assert d1 == d2
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert len(files) == 1  # one object, no duplicate, no leftover .tmp


def test_distinct_content_distinct_digest(tmp_path):
    store = ContentAddressedStore(tmp_path)
    assert store.put(b"a") != store.put(b"b")


def test_get_missing_raises_keyerror(tmp_path):
    with pytest.raises(KeyError):
        ContentAddressedStore(tmp_path).get(sha256_hex(b"never stored"))


def test_read_detects_on_disk_tamper(tmp_path):
    store = ContentAddressedStore(tmp_path)
    digest = store.put(b"trusted content")
    # tamper with the stored bytes under the content-addressed name
    path = store._path_for(digest)
    path.write_bytes(b"swapped content")
    with pytest.raises(ObjectIntegrityError):
        store.get(digest)
    assert store.verify(digest) is False


def test_exists_and_verify_on_intact_object(tmp_path):
    store = ContentAddressedStore(tmp_path)
    digest = store.put(b"intact")
    assert store.exists(digest) is True
    assert store.verify(digest) is True


def test_successful_put_leaves_no_tmp_files(tmp_path):
    store = ContentAddressedStore(tmp_path)
    store.put(b"x")
    assert [p.name for p in tmp_path.rglob("*.tmp")] == []
