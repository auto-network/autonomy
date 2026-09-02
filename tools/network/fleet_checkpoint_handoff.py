"""Quiesced, recoverable publication of received personal checkpoints."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from tools.network.fleet_sync_connection import (
    DatabaseQuiescence,
    require_database_quiescence,
)
from tools.network.fleet_sync.sync import (
    AlphaCheckpoint,
    AlphaError,
    _fsync_directory,
    _read_manifest,
    install_checkpoint,
)
from tools.network.fleet_sync.materialize import (
    ContentAddressedBlobStore,
    production_blob_store,
)


HANDOFF_MARKER_VERSION = 1


def _marker_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.fleet-sync-handoff.json")


def _backup_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.pre-fleet-sync")


def _inode(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def _integrity_ok(path: Path) -> bool:
    import sqlite3

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except (OSError, sqlite3.Error):
        return False


def _write_marker(target: Path, payload: dict[str, object]) -> None:
    marker = _marker_path(target)
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    temporary = marker.with_name(marker.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(marker)
    _fsync_directory(target.parent)


def recover_checkpoint_handoff(
    target_path: Path,
    *,
    quiescence: DatabaseQuiescence,
) -> str:
    """Resolve a publication interrupted before or after the atomic swap."""
    target = Path(target_path)
    require_database_quiescence(quiescence, target)
    marker = _marker_path(target)
    backup = _backup_path(target)
    before: tuple[int, int] | None = None
    if marker.exists():
        try:
            payload = json.loads(marker.read_bytes())
            if (
                not isinstance(payload, dict)
                or payload.get("version") != HANDOFF_MARKER_VERSION
            ):
                raise ValueError
            raw_before = payload.get("target_inode_before")
            if raw_before is not None:
                if (
                    not isinstance(raw_before, list)
                    or len(raw_before) != 2
                    or not all(isinstance(value, int) for value in raw_before)
                ):
                    raise ValueError
                before = int(raw_before[0]), int(raw_before[1])
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise AlphaError("checkpoint handoff marker is malformed") from exc

    current = _inode(target)
    published = marker.exists() and current is not None and current != before
    outcome = "clean"
    if published:
        if not _integrity_ok(target):
            if not backup.exists():
                raise AlphaError(
                    "interrupted checkpoint is corrupt and has no prior database"
                )
            target.unlink(missing_ok=True)
            os.replace(backup, target)
            outcome = "restored"
        else:
            backup.unlink(missing_ok=True)
            outcome = "published"
    elif backup.exists():
        target.unlink(missing_ok=True)
        os.replace(backup, target)
        outcome = "restored"

    marker.unlink(missing_ok=True)
    marker.with_name(marker.name + ".tmp").unlink(missing_ok=True)
    for temporary in target.parent.glob(
        f".fleet-sync-install-{target.name}-*"
    ):
        if temporary.is_dir():
            shutil.rmtree(temporary, ignore_errors=True)
    _fsync_directory(target.parent)
    return outcome


def install_quiesced_checkpoint(
    checkpoint_directory: Path,
    target_path: Path,
    *,
    quiescence: DatabaseQuiescence,
    target_origin_incarnation: str,
    expected_roster_epoch: int | str,
    expected_active_roster: tuple[str, ...],
    source_machine_pub: str | None = None,
    blob_store: ContentAddressedBlobStore | None = None,
) -> AlphaCheckpoint:
    """Merge local winners, publish atomically, and leave crash recovery proof."""
    target = Path(target_path)
    require_database_quiescence(quiescence, target)
    if source_machine_pub is not None and (
        source_machine_pub not in expected_active_roster
        or source_machine_pub == target_origin_incarnation
    ):
        raise AlphaError("checkpoint source is not an active remote fleet machine")
    if _marker_path(target).exists() or _backup_path(target).exists():
        recover_checkpoint_handoff(target, quiescence=quiescence)
    manifest, digest = _read_manifest(Path(checkpoint_directory))
    if blob_store is None:
        # The machine's own attachment store: realizes rows whose bytes a
        # local file already satisfies (the pre-swap database is the
        # candidate index); everything else defers to quarantine for the
        # attachment transport to drain.
        blob_store = production_blob_store(target, extra_source=target)
    target.parent.mkdir(parents=True, exist_ok=True)
    before = _inode(target)
    _write_marker(target, {
        "version": HANDOFF_MARKER_VERSION,
        "target_inode_before": list(before) if before is not None else None,
        "manifest_sha256": digest,
        "watermark": manifest.get("watermark"),
    })
    try:
        installed = install_checkpoint(
            Path(checkpoint_directory),
            target,
            target_origin_incarnation=target_origin_incarnation,
            expected_roster_epoch=expected_roster_epoch,
            expected_active_roster=expected_active_roster,
            blob_store=blob_store,
            merge_existing=True,
            checkpoint_source_machine=source_machine_pub,
        )
    except Exception:
        recover_checkpoint_handoff(target, quiescence=quiescence)
        raise
    _marker_path(target).unlink(missing_ok=True)
    _fsync_directory(target.parent)
    return installed
