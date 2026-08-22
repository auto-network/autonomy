"""Portable, fail-closed node-volume snapshot and restore.

Version 1 deliberately requires a quiesced node. SQLite's backup API gives
each database a consistent image, but without a cross-store writer barrier it
cannot make several databases and loose key/config files share one hot point
in time. The explicit acknowledgement keeps that limitation honest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator
from urllib.parse import quote

from tools.data_paths import (
    REFUSE_REAL_DATA_FALLBACK_ENV,
    STORE_MANIFEST,
    resolve_store,
)


SNAPSHOT_FORMAT_VERSION = 1
VOLUME_SCHEMA_VERSION = 1
SNAPSHOT_MANIFEST = "snapshot-manifest.json"
VOLUME_PAYLOAD = "volume"
BEADS_PAYLOAD = "external/beads.sql"
VOLUME_STAMP = ".autonomy-volume.json"


class PortabilityError(RuntimeError):
    """A snapshot, restore, or migration contract was refused."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative_to(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise PortabilityError(f"path escapes the volume: {path}") from exc


def _assert_no_symlink(path: Path, *, root: Path) -> None:
    current = path
    while current != root:
        if current.is_symlink():
            raise PortabilityError(
                f"refusing symlink in portable volume: {_relative_to(current, root)}"
            )
        current = current.parent


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{quote(str(source), safe='/')}?mode=ro"
    try:
        source_conn = sqlite3.connect(source_uri, uri=True, timeout=5)
        target_conn = sqlite3.connect(str(target))
        try:
            source_conn.backup(target_conn)
            result = target_conn.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise PortabilityError(
                    f"SQLite integrity check failed for {source}: "
                    f"{result[0] if result else 'no result'}"
                )
        finally:
            target_conn.close()
            source_conn.close()
    except sqlite3.Error as exc:
        raise PortabilityError(f"could not snapshot SQLite store {source}: {exc}") from exc
    shutil.copymode(source, target)


def _copy_regular(source: Path, target: Path, *, root: Path) -> None:
    _assert_no_symlink(source, root=root)
    if not source.is_file():
        raise PortabilityError(
            f"refusing non-regular volume entry: {_relative_to(source, root)}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_directory(
    source: Path,
    target: Path,
    *,
    root: Path,
    sqlite_children: bool,
) -> None:
    _assert_no_symlink(source, root=root)
    target.mkdir(parents=True, exist_ok=True)
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        destination = target / relative
        _assert_no_symlink(item, root=root)
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copymode(item, destination)
            continue
        if not item.is_file():
            raise PortabilityError(
                f"refusing non-regular volume entry: {_relative_to(item, root)}"
            )
        if sqlite_children and (
            item.name.endswith("-wal") or item.name.endswith("-shm")
        ):
            # The SQLite backup contains the committed WAL state. Copying the
            # sidecars would create a torn or replay-dependent restore.
            continue
        if sqlite_children and item.suffix == ".db":
            _sqlite_backup(item, destination)
        else:
            _copy_regular(item, destination, root=root)


def _resolved_store_path(volume_root: Path, key: str, relative: str) -> Path:
    expected = (volume_root / relative).resolve()
    resolved = resolve_store(key, root=volume_root).resolve()
    if resolved != expected:
        raise PortabilityError(
            f"store {key!r} resolves outside the selected volume: "
            f"{resolved} != {expected}"
        )
    return resolved


def _file_rows(payload_root: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(p for p in payload_root.rglob("*") if p.is_file()):
        _assert_no_symlink(path, root=payload_root)
        rows.append({
            "path": path.relative_to(payload_root).as_posix(),
            "sha256": _sha256(path),
            "size": path.stat().st_size,
            "mode": stat.S_IMODE(path.stat().st_mode),
        })
    return rows


def _snapshot_id(manifest_without_id: dict) -> str:
    return hashlib.sha256(_canonical_json(manifest_without_id)).hexdigest()


def _read_volume_version(volume_root: Path) -> int:
    stamp = volume_root / VOLUME_STAMP
    if not stamp.exists():
        return 0
    if stamp.is_symlink() or not stamp.is_file():
        raise PortabilityError(f"invalid volume version stamp: {stamp}")
    try:
        payload = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PortabilityError(f"unreadable volume version stamp: {exc}") from exc
    if payload.get("format") != "autonomy-volume":
        raise PortabilityError("volume version stamp has an unknown format")
    version = payload.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise PortabilityError("volume version stamp has an invalid schema_version")
    return version


def create_snapshot(
    volume_root: Path | str,
    artifact: Path | str,
    *,
    quiesced: bool,
    beads_present: bool = False,
    beads_dump: Path | str | None = None,
) -> dict:
    """Create one portable artifact from a quiesced node data volume."""
    if not quiesced:
        raise PortabilityError(
            "portable snapshots require a quiesced node; stop the dashboard "
            "and pass --quiesced"
        )
    root = Path(volume_root).resolve()
    output = Path(artifact).resolve()
    if not root.is_dir():
        raise PortabilityError(f"volume root is not a directory: {root}")
    if output.exists():
        raise PortabilityError(f"refusing to overwrite snapshot artifact: {output}")
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise PortabilityError("snapshot artifact must be outside the source volume")
    orgs_path = _resolved_store_path(root, "orgs", "orgs")
    personal_homes = (orgs_path.parent / "personal.db", orgs_path / "personal.db")
    if not any(p.is_file() for p in personal_homes):
        raise PortabilityError(
            "selected volume is not an initialized node "
            "(the personal store is required)"
        )

    dump_path = Path(beads_dump).resolve() if beads_dump is not None else None
    if beads_present and dump_path is None:
        raise PortabilityError(
            "beads is declared present but no consistent --beads-dump was supplied"
        )
    if dump_path is not None:
        if not dump_path.is_file() or dump_path.is_symlink():
            raise PortabilityError(f"invalid beads dump: {dump_path}")
        beads_present = True

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".autonomy-snapshot-",
        dir=str(output.parent),
    ) as temporary:
        stage = Path(temporary)
        payload = stage / VOLUME_PAYLOAD
        payload.mkdir()
        stores: list[dict] = []

        for store in STORE_MANIFEST:
            source = _resolved_store_path(root, store.key, store.relative)
            destination = payload / store.relative
            present = source.exists()
            stores.append({
                "key": store.key,
                "relative": store.relative,
                "kind": store.kind,
                "present": present,
            })
            if not present:
                continue
            if store.kind == "db":
                _assert_no_symlink(source, root=root)
                _sqlite_backup(source, destination)
            elif store.kind == "file":
                _copy_regular(source, destination, root=root)
            elif store.kind == "dir":
                _copy_directory(
                    source,
                    destination,
                    root=root,
                    sqlite_children=(store.key == "orgs"),
                )
            else:
                raise PortabilityError(
                    f"unsupported store kind {store.kind!r} for {store.key!r}"
                )

        stamp = root / VOLUME_STAMP
        if stamp.exists():
            _copy_regular(stamp, payload / VOLUME_STAMP, root=root)

        beads: dict = {"present": beads_present}
        if dump_path is not None:
            beads_target = stage / BEADS_PAYLOAD
            beads_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dump_path, beads_target)
            beads.update({
                "path": BEADS_PAYLOAD,
                "sha256": _sha256(beads_target),
                "size": beads_target.stat().st_size,
            })

        manifest: dict = {
            "format": "autonomy-node-snapshot",
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "created_at": _utc_now(),
            "consistency": "quiesced",
            "volume_schema_version": _read_volume_version(root),
            "stores": stores,
            "files": _file_rows(payload),
            "beads": beads,
        }
        manifest["snapshot_id"] = _snapshot_id(manifest)
        (stage / SNAPSHOT_MANIFEST).write_bytes(_canonical_json(manifest) + b"\n")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=str(output.parent),
        )
        os.close(descriptor)
        temporary_artifact = Path(temporary_name)
        try:
            with tarfile.open(temporary_artifact, "w:gz") as archive:
                archive.add(stage / SNAPSHOT_MANIFEST, arcname=SNAPSHOT_MANIFEST)
                archive.add(payload, arcname=VOLUME_PAYLOAD)
                if dump_path is not None:
                    archive.add(stage / "external", arcname="external")
            try:
                os.link(temporary_artifact, output)
            except FileExistsError as exc:
                raise PortabilityError(
                    f"refusing to overwrite snapshot artifact: {output}"
                ) from exc
            temporary_artifact.unlink()
        finally:
            temporary_artifact.unlink(missing_ok=True)
    return manifest


def _safe_extract(artifact: Path, destination: Path) -> None:
    try:
        archive = tarfile.open(artifact, "r:*")
    except (OSError, tarfile.TarError) as exc:
        raise PortabilityError(f"unreadable snapshot artifact: {exc}") from exc
    with archive:
        seen: set[str] = set()
        for member in archive.getmembers():
            pure = PurePosixPath(member.name)
            canonical_name = pure.as_posix()
            if member.name != canonical_name:
                raise PortabilityError(
                    f"non-canonical path in snapshot artifact: {member.name!r}"
                )
            if canonical_name in seen:
                raise PortabilityError(
                    f"duplicate path in snapshot artifact: {member.name!r}"
                )
            seen.add(canonical_name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or not pure.parts
                or pure.parts[0] not in {SNAPSHOT_MANIFEST, VOLUME_PAYLOAD, "external"}
                or (
                    pure.parts[0] == SNAPSHOT_MANIFEST
                    and len(pure.parts) != 1
                )
                or (
                    pure.parts[0] == "external"
                    and pure.as_posix() not in {"external", BEADS_PAYLOAD}
                )
            ):
                raise PortabilityError(
                    f"unsafe path in snapshot artifact: {member.name!r}"
                )
            if member.issym() or member.islnk() or member.isdev():
                raise PortabilityError(
                    f"refusing linked/device artifact member: {member.name!r}"
                )
            target = destination.joinpath(*pure.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(member.mode & 0o777)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise PortabilityError(
                        f"could not read artifact member: {member.name!r}"
                    )
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(member.mode & 0o777)
            else:
                raise PortabilityError(
                    f"unsupported artifact member: {member.name!r}"
                )


def _load_and_validate_snapshot(stage: Path) -> dict:
    manifest_path = stage / SNAPSHOT_MANIFEST
    payload_root = stage / VOLUME_PAYLOAD
    if not manifest_path.is_file() or not payload_root.is_dir():
        raise PortabilityError("snapshot is missing its manifest or volume payload")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PortabilityError(f"snapshot manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PortabilityError("snapshot manifest must be a JSON object")
    if (
        manifest.get("format") != "autonomy-node-snapshot"
        or manifest.get("format_version") != SNAPSHOT_FORMAT_VERSION
        or manifest.get("consistency") != "quiesced"
    ):
        raise PortabilityError("unsupported snapshot format or consistency mode")
    claimed_id = manifest.get("snapshot_id")
    body = dict(manifest)
    body.pop("snapshot_id", None)
    if not isinstance(claimed_id, str) or claimed_id != _snapshot_id(body):
        raise PortabilityError("snapshot manifest identity does not verify")
    snapshot_volume_version = manifest.get("volume_schema_version")
    if (
        not isinstance(snapshot_volume_version, int)
        or isinstance(snapshot_volume_version, bool)
        or snapshot_volume_version < 0
        or snapshot_volume_version != _read_volume_version(payload_root)
    ):
        raise PortabilityError(
            "snapshot volume schema version does not match its payload"
        )

    expected_stores = [
        (store.key, store.relative, store.kind) for store in STORE_MANIFEST
    ]
    store_rows = manifest.get("stores", [])
    if not isinstance(store_rows, list) or not all(
        isinstance(row, dict) for row in store_rows
    ):
        raise PortabilityError("snapshot has an invalid store manifest")
    actual_stores = [
        (row.get("key"), row.get("relative"), row.get("kind"))
        for row in store_rows
    ]
    if actual_stores != expected_stores:
        raise PortabilityError("snapshot store manifest does not match this node")
    for store, row in zip(STORE_MANIFEST, store_rows, strict=True):
        present = row.get("present")
        if not isinstance(present, bool):
            raise PortabilityError(
                f"snapshot store {store.key!r} has an invalid presence marker"
            )
        store_path = payload_root / store.relative
        if present != store_path.exists():
            raise PortabilityError(
                f"snapshot store {store.key!r} presence does not match its payload"
            )
        if present and store.kind == "dir" and not store_path.is_dir():
            raise PortabilityError(
                f"snapshot store {store.key!r} is not a directory"
            )
        if present and store.kind in {"db", "file"} and not store_path.is_file():
            raise PortabilityError(f"snapshot store {store.key!r} is not a file")

    expected_files: dict[str, dict] = {}
    for row in manifest.get("files", []):
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise PortabilityError("snapshot contains an invalid file manifest row")
        path = row["path"]
        if path in expected_files:
            raise PortabilityError(f"duplicate snapshot file row: {path}")
        expected_files[path] = row
    actual_files = {
        path.relative_to(payload_root).as_posix(): path
        for path in payload_root.rglob("*")
        if path.is_file()
    }
    if set(actual_files) != set(expected_files):
        raise PortabilityError("snapshot payload is torn or has unmanifested files")
    for relative, path in actual_files.items():
        row = expected_files[relative]
        if path.is_symlink():
            raise PortabilityError(f"snapshot payload contains a symlink: {relative}")
        if row.get("size") != path.stat().st_size or row.get("sha256") != _sha256(path):
            raise PortabilityError(f"snapshot payload hash mismatch: {relative}")
        if row.get("mode") != stat.S_IMODE(path.stat().st_mode):
            raise PortabilityError(f"snapshot payload mode mismatch: {relative}")

    sqlite_paths = {
        store.relative
        for store in STORE_MANIFEST
        if store.kind == "db"
    }
    sqlite_paths.update(
        relative
        for relative in actual_files
        if relative.startswith("orgs/") and relative.endswith(".db")
    )
    for relative in sorted(sqlite_paths & actual_files.keys()):
        try:
            conn = sqlite3.connect(
                f"file:{quote(str(actual_files[relative]), safe='/')}?mode=ro",
                uri=True,
            )
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise PortabilityError(
                f"restored SQLite store is unreadable: {relative}: {exc}"
            ) from exc
        if result is None or result[0] != "ok":
            raise PortabilityError(
                f"restored SQLite integrity check failed: {relative}"
            )

    beads = manifest.get("beads")
    if not isinstance(beads, dict) or not isinstance(beads.get("present"), bool):
        raise PortabilityError("snapshot has an invalid beads declaration")
    beads_path = stage / BEADS_PAYLOAD
    if beads["present"]:
        if (
            not beads_path.is_file()
            or beads.get("path") != BEADS_PAYLOAD
            or beads.get("size") != beads_path.stat().st_size
            or beads.get("sha256") != _sha256(beads_path)
        ):
            raise PortabilityError("snapshot beads dump is missing or torn")
    elif beads_path.exists():
        raise PortabilityError("snapshot carries an undeclared beads dump")
    return manifest


def restore_snapshot(
    artifact: Path | str,
    target_volume: Path | str,
    *,
    beads_output: Path | str | None = None,
) -> dict:
    """Validate and restore an artifact into a fresh volume path."""
    source = Path(artifact).resolve()
    target = Path(target_volume).resolve()
    if not source.is_file():
        raise PortabilityError(f"snapshot artifact not found: {source}")
    target_preexists = target.exists()
    if target_preexists and any(target.iterdir()):
        raise PortabilityError(f"restore target must be fresh and empty: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=".autonomy-restore-",
        dir=str(target.parent),
    ) as temporary:
        stage = Path(temporary)
        staged_beads_output: Path | None = None
        _safe_extract(source, stage)
        manifest = _load_and_validate_snapshot(stage)
        beads = manifest["beads"]
        requested_beads = Path(beads_output).resolve() if beads_output else None
        if requested_beads is not None:
            try:
                requested_beads.relative_to(target)
            except ValueError:
                pass
            else:
                raise PortabilityError(
                    "beads output must be outside the restored node volume"
                )
        if beads["present"] and requested_beads is None:
            raise PortabilityError(
                "snapshot contains beads state; pass --beads-output so it is not lost"
            )
        if requested_beads is not None:
            if not beads["present"]:
                raise PortabilityError(
                    "--beads-output was supplied but the snapshot has no beads state"
                )
            if requested_beads.exists():
                raise PortabilityError(
                    f"refusing to overwrite beads output: {requested_beads}"
                )
            requested_beads.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{requested_beads.name}.",
                suffix=".tmp",
                dir=str(requested_beads.parent),
            )
            os.close(descriptor)
            staged_beads_output = Path(temporary_name)
            try:
                shutil.copy2(stage / BEADS_PAYLOAD, staged_beads_output)
                if _sha256(staged_beads_output) != beads["sha256"]:
                    raise PortabilityError(
                        "beads dump changed while staging its restore"
                    )
            except Exception:
                staged_beads_output.unlink(missing_ok=True)
                raise

        restored_payload = stage / VOLUME_PAYLOAD
        try:
            if target_preexists:
                # An existing EMPTY target is a mounted volume (e.g. the
                # container's /app/data): its mount point cannot be replaced
                # by rename (EBUSY) and it sits on a different filesystem than
                # the stage (EXDEV). Fill it child-by-child rather than
                # replacing the directory itself.
                for child in restored_payload.iterdir():
                    shutil.move(str(child), str(target / child.name))
            else:
                os.replace(restored_payload, target)
            if requested_beads is not None and staged_beads_output is not None:
                try:
                    # The temporary and final dump share a filesystem.
                    # Hard-linking publishes without a race-prone overwrite,
                    # even when node and Dolt volumes are different mounts.
                    os.link(staged_beads_output, requested_beads)
                except Exception as exc:
                    # Keep the restore all-or-nothing for expected publication
                    # failures: the payload can move back because stage and
                    # target are on the same filesystem.
                    try:
                        os.replace(target, restored_payload)
                    except OSError as rollback_exc:
                        raise PortabilityError(
                            "could not publish the beads dump and could not "
                            f"roll back the node volume: {rollback_exc}"
                        ) from rollback_exc
                    raise PortabilityError(
                        f"could not publish the beads dump: {exc}"
                    ) from exc
        finally:
            if staged_beads_output is not None:
                staged_beads_output.unlink(missing_ok=True)
    return manifest


@contextmanager
def _root_volume_stores(volume_root: Path) -> Iterator[None]:
    saved: dict[str, str | None] = {}
    # The ambient base roots everything that composes with it — including
    # the graph store, which must NEVER be pinned whole: a GRAPH_DB pin
    # collapses org resolution to one file and conflicts with the join and
    # bootstrap flows' explicit org='personal' writes under the fail-loud
    # resolver (this was the root cause of the join-suite failures).
    from tools.data_paths import DATA_ROOT_ENV

    saved[DATA_ROOT_ENV] = os.environ.get(DATA_ROOT_ENV)
    os.environ[DATA_ROOT_ENV] = str(volume_root)
    # GRAPH_DB is a whole-DB pin, not a store location: never point it at
    # the volume — clearing it lets per-org resolution route normally.
    saved["GRAPH_DB"] = os.environ.get("GRAPH_DB")
    os.environ.pop("GRAPH_DB", None)
    for store in STORE_MANIFEST:
        if store.env:
            saved[store.env] = os.environ.get(store.env)
            os.environ[store.env] = str(volume_root / store.relative)
    saved[REFUSE_REAL_DATA_FALLBACK_ENV] = os.environ.get(
        REFUSE_REAL_DATA_FALLBACK_ENV
    )
    os.environ[REFUSE_REAL_DATA_FALLBACK_ENV] = "1"
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def migrate_on_mount(
    volume_root: Path | str,
    *,
    supported_version: int = VOLUME_SCHEMA_VERSION,
    tls: bool = True,
    join_transport=None,
) -> dict:
    """Fail on newer volumes, then run every existing forward initializer."""
    root = Path(volume_root).resolve()
    if root.exists() and not root.is_dir():
        raise PortabilityError(f"volume mount is not a directory: {root}")
    current = _read_volume_version(root) if root.exists() else 0
    if current > supported_version:
        raise PortabilityError(
            f"volume schema v{current} is newer than this node's "
            f"supported v{supported_version}; refusing before migration"
        )

    root.mkdir(parents=True, exist_ok=True)
    from tools.init.first_run import initialize_data_root

    with _root_volume_stores(root):
        report = initialize_data_root(
            root,
            invite=os.environ.get("AUTONOMY_INVITE"),
            join_transport=join_transport,
            fleet_invite=os.environ.get("AUTONOMY_FLEET_INVITE"),
            tls=tls,
        )

    if current != supported_version:
        stamp = {
            "format": "autonomy-volume",
            "schema_version": supported_version,
            "migrated_at": _utc_now(),
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".autonomy-volume.",
            suffix=".tmp",
            dir=str(root),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_canonical_json(stamp) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, root / VOLUME_STAMP)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "from_version": current,
        "to_version": supported_version,
        "changed": current != supported_version or report.changed,
        "steps": [step.to_dict() for step in report.steps],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.portability",
        description="Quiesced node-volume snapshot, restore, and mount migration",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="create a portable snapshot")
    snapshot.add_argument("volume", help="node data-volume root")
    snapshot.add_argument("artifact", help="new .tar.gz artifact path")
    snapshot.add_argument(
        "--quiesced",
        action="store_true",
        help="acknowledge that all node and Dolt writers are stopped",
    )
    snapshot.add_argument(
        "--beads-present",
        action="store_true",
        help="declare optional Dolt/beads state present (requires --beads-dump)",
    )
    snapshot.add_argument(
        "--beads-dump",
        help="consistent beads.sql exported from the quiesced Dolt volume",
    )

    restore = sub.add_parser("restore", help="restore into an absent fresh volume")
    restore.add_argument("artifact", help="snapshot .tar.gz")
    restore.add_argument("volume", help="fresh target volume path")
    restore.add_argument(
        "--beads-output",
        help="required output path when the artifact contains beads.sql",
    )

    migrate = sub.add_parser(
        "migrate-on-mount",
        help="refuse newer volumes and run forward schema initializers",
    )
    migrate.add_argument("volume", help="mounted node data-volume root")
    migrate.add_argument("--no-tls", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = create_snapshot(
                args.volume,
                args.artifact,
                quiesced=args.quiesced,
                beads_present=args.beads_present,
                beads_dump=args.beads_dump,
            )
        elif args.command == "restore":
            result = restore_snapshot(
                args.artifact,
                args.volume,
                beads_output=args.beads_output,
            )
        else:
            result = migrate_on_mount(args.volume, tls=not args.no_tls)
    except PortabilityError as exc:
        print(f"portability: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
