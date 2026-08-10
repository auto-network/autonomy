"""The host's X25519 recipient key for HPKE-sealed secure-setting provisioning.

One private key file on the host — ``data/repl-login.key`` by default,
relocatable via ``REPL_LOGIN_KEY_FILE`` (store ``repl_login_key`` in the
volume manifest). The dashboard only ever derives the PUBLIC key from it:
the operator's browser seals credential payloads to that public key
(``static/js/ceremony/sealing.js``), the ``secure_setting`` approval
executor stores the resulting ciphertext, and the host-side consumer that
owns this file opens it with :func:`tools.network.idkit.seal_open`. The
dashboard process never decrypts a sealed payload and never logs key
material.

File format: one line of 64 lowercase hex characters — the raw X25519
private scalar, exactly what ``idkit`` sealing takes as
``recipient_private_key``. Created lazily on first use with 0600 and
``O_EXCL`` (concurrent workers race safely: first creator wins, losers
read the winner's key back). A pre-existing file with loose permissions
is re-clamped to 0600 on read.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tools.data_paths import resolve_store

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

#: Length of the key-id — a stable public fingerprint, not a secret.
KEY_ID_HEX_CHARS = 16


def key_file_path() -> Path:
    """Where the private key lives (env ``REPL_LOGIN_KEY_FILE`` outranks
    the repo-local default, like every manifest store)."""
    return resolve_store("repl_login_key")


def key_id_for(public_hex: str) -> str:
    """Stable fingerprint of an encapsulation public key: the first 16 hex
    chars of SHA-256 over the raw public key bytes."""
    return hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:KEY_ID_HEX_CHARS]


def _clamp_mode(path: Path) -> None:
    """Re-clamp a pre-existing key file to 0600 if group/other bits leaked."""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        os.chmod(path, 0o600)


def _load_private_hex(path: Path) -> str:
    raw = path.read_text().strip()
    if not _HEX64_RE.fullmatch(raw):
        raise ValueError(
            f"the recipient key file {path} does not hold 64 lowercase hex "
            "characters — refusing to use it"
        )
    _clamp_mode(path)
    return raw


def recipient_public_key() -> tuple[str, str]:
    """The public half of the host recipient key as ``(public_hex, key_id)``.

    Generates the private key file on first use. Raises ``ValueError`` when
    an existing file is unreadable or malformed — never silently replaces
    key material an operator may depend on.
    """
    path = key_file_path()
    if path.exists():
        private_hex = _load_private_hex(path)
    else:
        fresh = X25519PrivateKey.generate().private_bytes_raw().hex()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, fresh.encode("ascii"))
            finally:
                os.close(fd)
            private_hex = fresh
        except FileExistsError:
            private_hex = _load_private_hex(path)
    public_hex = (
        X25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
        .public_key()
        .public_bytes_raw()
        .hex()
    )
    return public_hex, key_id_for(public_hex)
