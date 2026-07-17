"""Content-addressed block store — the unit of swarm transfer (G2, §8).

An artifact is fixed-size blocks plus a **manifest** naming every
block's SHA-256. The artifact id is the SHA-256 of the canonical-JSON
manifest, so the id commits to the whole block list: a manifest fetched
from ANY peer is verifiable against the id alone, and every block is
verifiable against the manifest alone. Nothing about integrity depends
on who served the bytes — that is what makes the transport untrusted
and every org peer an equally good source.

Have-maps travel as hex bitmaps (LSB-first within each byte), compact
enough to poll: a 10 GiB artifact at 256 KiB blocks is a 5 KiB bitmap.
"""

from __future__ import annotations

import hashlib
from typing import Dict, Optional, Set

from tools.network.idkit import canonical_json

BLOCK_SIZE = 256 * 1024
MANIFEST_VERSION = 1
HASH_ALGO = "sha256"
MAX_BLOCKS = 1 << 22  # 1 TiB at the default block size; shape sanity, not policy


class BlockError(Exception):
    """A manifest or block failed validation."""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(data: bytes, block_size: int = BLOCK_SIZE) -> dict:
    """Cut *data* into blocks and describe them.

    The final block may be short; every other block is exactly
    *block_size* — receivers use that to validate block lengths before
    hashing.
    """
    if block_size <= 0:
        raise BlockError("block_size must be positive")
    if not data:
        raise BlockError("empty artifacts have no manifest")
    blocks = [
        _sha256_hex(data[i:i + block_size]) for i in range(0, len(data), block_size)
    ]
    return {
        "v": MANIFEST_VERSION,
        "algo": HASH_ALGO,
        "size": len(data),
        "block_size": block_size,
        "blocks": blocks,
    }


def manifest_id(manifest: dict) -> str:
    """The artifact id: SHA-256 over the canonical-JSON manifest."""
    return _sha256_hex(canonical_json(manifest))


def check_manifest(manifest: object) -> dict:
    """Validate manifest shape; returns it or raises :class:`BlockError`."""
    if not isinstance(manifest, dict):
        raise BlockError("manifest must be an object")
    if set(manifest) != {"v", "algo", "size", "block_size", "blocks"}:
        raise BlockError("manifest has wrong fields")
    if manifest["v"] != MANIFEST_VERSION:
        raise BlockError("unsupported manifest version")
    if manifest["algo"] != HASH_ALGO:
        raise BlockError("unsupported hash algorithm")
    size, block_size, blocks = manifest["size"], manifest["block_size"], manifest["blocks"]
    if not isinstance(size, int) or not isinstance(block_size, int):
        raise BlockError("size fields must be integers")
    if size <= 0 or block_size <= 0:
        raise BlockError("size fields must be positive")
    if not isinstance(blocks, list) or not blocks or len(blocks) > MAX_BLOCKS:
        raise BlockError("blocks must be a non-empty list")
    expected = (size + block_size - 1) // block_size
    if len(blocks) != expected:
        raise BlockError("block count does not match size")
    for h in blocks:
        if not isinstance(h, str) or len(h) != 64 or h != h.lower():
            raise BlockError("block hashes must be 64 lowercase hex chars")
        try:
            bytes.fromhex(h)
        except ValueError as exc:
            raise BlockError("block hashes must be hex") from exc
    return manifest


def block_length(manifest: dict, index: int) -> int:
    """The exact byte length block *index* must have."""
    size, block_size = manifest["size"], manifest["block_size"]
    if index == len(manifest["blocks"]) - 1:
        return size - block_size * index
    return block_size


def indices_to_bitmap_hex(indices: Set[int], total: int) -> str:
    """Encode a have-set as an LSB-first hex bitmap of *total* bits."""
    buf = bytearray((total + 7) // 8)
    for i in indices:
        if 0 <= i < total:
            buf[i // 8] |= 1 << (i % 8)
    return bytes(buf).hex()


def bitmap_hex_to_indices(bitmap: str, total: int) -> Set[int]:
    """Decode an LSB-first hex bitmap; raises :class:`BlockError` on junk."""
    if not isinstance(bitmap, str):
        raise BlockError("bitmap must be a hex string")
    try:
        buf = bytes.fromhex(bitmap)
    except ValueError as exc:
        raise BlockError("bitmap must be hex") from exc
    if len(buf) != (total + 7) // 8:
        raise BlockError("bitmap length does not match block count")
    out: Set[int] = set()
    for i in range(total):
        if buf[i // 8] >> (i % 8) & 1:
            out.add(i)
    return out


class BlockStore:
    """In-memory manifests + blocks for any number of artifacts.

    Every write path verifies: :meth:`add_manifest` recomputes the
    artifact id, :meth:`add_block` recomputes the block hash. A store
    can therefore never hold bytes that disagree with the ids it
    advertises — serving peers are honest by construction, and a
    corrupting peer is caught by the *receiver's* identical checks.
    """

    def __init__(self) -> None:
        self._manifests: Dict[str, dict] = {}
        self._blocks: Dict[str, Dict[int, bytes]] = {}

    # ── publisher path ──────────────────────────────────────────────

    def add_artifact(self, data: bytes, block_size: int = BLOCK_SIZE) -> str:
        """Seed a complete artifact; returns its id."""
        manifest = build_manifest(data, block_size)
        artifact_id = manifest_id(manifest)
        self._manifests[artifact_id] = manifest
        self._blocks[artifact_id] = {
            i: data[off:off + block_size]
            for i, off in enumerate(range(0, len(data), block_size))
        }
        return artifact_id

    # ── fetcher path ────────────────────────────────────────────────

    def add_manifest(self, artifact_id: str, manifest: object) -> dict:
        """Accept a manifest iff it hashes to *artifact_id*."""
        manifest = check_manifest(manifest)
        if manifest_id(manifest) != artifact_id:
            raise BlockError("manifest does not match artifact id")
        self._manifests.setdefault(artifact_id, manifest)
        self._blocks.setdefault(artifact_id, {})
        return manifest

    def add_block(self, artifact_id: str, index: int, data: bytes) -> bool:
        """Store a block iff it verifies; False = corrupt (caller's move)."""
        manifest = self._manifests.get(artifact_id)
        if manifest is None:
            raise BlockError("unknown artifact")
        if not 0 <= index < len(manifest["blocks"]):
            raise BlockError("block index out of range")
        if len(data) != block_length(manifest, index):
            return False
        if _sha256_hex(data) != manifest["blocks"][index]:
            return False
        self._blocks[artifact_id][index] = data
        return True

    # ── reads ───────────────────────────────────────────────────────

    def manifest(self, artifact_id: str) -> Optional[dict]:
        return self._manifests.get(artifact_id)

    def have(self, artifact_id: str) -> Set[int]:
        return set(self._blocks.get(artifact_id, ()))

    def get_block(self, artifact_id: str, index: int) -> Optional[bytes]:
        return self._blocks.get(artifact_id, {}).get(index)

    def is_complete(self, artifact_id: str) -> bool:
        manifest = self._manifests.get(artifact_id)
        return (
            manifest is not None
            and len(self._blocks[artifact_id]) == len(manifest["blocks"])
        )

    def assemble(self, artifact_id: str) -> bytes:
        """The whole artifact; raises unless complete."""
        if not self.is_complete(artifact_id):
            raise BlockError("artifact is incomplete")
        blocks = self._blocks[artifact_id]
        return b"".join(blocks[i] for i in range(len(blocks)))
