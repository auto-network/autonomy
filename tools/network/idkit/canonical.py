"""Canonical JSON encoding — the byte form every idkit signature covers.

Rules (fixed for v1; changing any of them is a breaking format change):

- keys sorted lexicographically at every nesting level
- separators ``(",", ":")`` — no whitespace
- ``ensure_ascii=True`` — output is pure ASCII, immune to unicode
  normalization disagreements between producers
- ``allow_nan=False`` — NaN/Infinity are rejected, never emitted
- only ``dict``/``list``/``str``/``int``/``bool``/``None`` values; floats are
  rejected because their textual form is not canonical across writers

Two structurally equal objects therefore always encode to bit-identical
bytes, which is what makes "deserialize → re-sign → identical signature"
hold (Ed25519 signing is itself deterministic).
"""

from __future__ import annotations

import json

from .errors import MalformedError


def _check_types(value: object) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise MalformedError("floats are not allowed in canonical idkit JSON")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise MalformedError("canonical JSON object keys must be strings")
            _check_types(v)
        return
    if isinstance(value, (list, tuple)):
        for v in value:
            _check_types(v)
        return
    raise MalformedError(f"type {type(value).__name__} is not allowed in canonical idkit JSON")


def canonical_json(obj: object) -> bytes:
    """Encode *obj* to canonical JSON bytes.

    Raises :class:`MalformedError` for values outside the canonical subset.
    """
    _check_types(obj)
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
