"""HTTP surface of the ``backup`` plugin.

Every read serves persisted Settings rows (machine-homed: backup state
is a fact about THIS machine's volume). Nothing here touches the
backup destination — the NAS is an NFS hard mount that has hung this
host before, so filesystem probes belong to the background reconciler
(bead auto-yj2wa), never a request handler (drivers S3/S7,
graph://7c45a180-345).

Authentication is the substrate's default-deny wrapper. Backup state
is machine-operational data every authenticated principal may read;
configuration writes require global authority (the operator's cookie
or a local host session) — an org-bound worker session does not get to
re-schedule the machine's backups.
"""
from __future__ import annotations

from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard.api_auth import principal_from_request
from tools.dashboard.plugins.backup.entrypoints.schemas import (
    BackupConfigV1,
    CONFIG_SET_ID,
    DRILL_SET_ID,
    RUN_SET_ID,
    SCHEMA_REVISION,
    TIERS,
)
from tools.graph.schemas.registry import SchemaValidationError

MACHINE = "machine"
CONFIG_KEY = "default"


def _config_defaults() -> dict:
    """The schema's declared defaults — one source, never a copy."""
    out = {}
    for name, meta in (BackupConfigV1._field_metadata or {}).items():
        if "default" in meta:
            out[name] = meta["default"]
        elif meta.get("default_factory"):
            out[name] = meta["default_factory"]()
    return out


def _read_config() -> dict:
    from tools.graph import settings_ops
    config = _config_defaults()
    try:
        row = settings_ops.read_set_key(
            CONFIG_SET_ID, CONFIG_KEY, org=MACHINE, peers=[])
    except Exception:
        row = None
    if row:
        payload = row.get("payload") if isinstance(row, dict) else None
        if isinstance(payload, dict):
            config.update(payload)
    return config


def _rows(set_id: str) -> list[dict]:
    from tools.graph import settings_ops
    try:
        members = settings_ops.read_set(set_id, org=MACHINE, peers=[])
    except Exception:
        return []
    return [{"key": m.key, **dict(m.payload or {})} for m in members]


def _parse_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _run_sort_key(run: dict) -> str:
    # The stamp segment of the key (tier:stamp) sorts chronologically.
    return run.get("key", "").split(":", 1)[-1]


def tier_health(runs: list[dict], config: dict, tier: str,
                now: datetime | None = None) -> dict:
    """Derive one tier's health from its run rows — the invariant, not
    scheduler activity (graph://c9fa6f75-481): staleness is the age of
    the newest COMPLETE capture against the expected interval, so a
    dead cron, an unmounted NAS, and a wedged engine all look the same:
    stale."""
    now = now or datetime.now(timezone.utc)
    tier_runs = sorted((r for r in runs if r.get("key", "").startswith(f"{tier}:")),
                       key=_run_sort_key, reverse=True)
    newest = tier_runs[0] if tier_runs else None
    newest_ok = next((r for r in tier_runs if r.get("verdict") == "complete"),
                     None)
    if tier == "hourly":
        interval_s = float(config.get("hourly_interval_minutes", 60)) * 60
    else:
        interval_s = 86400.0
    stale_after_s = float(config.get("staleness_multiple", 3.0)) * interval_s

    age_s = None
    if newest_ok:
        at = _parse_at(newest_ok.get("finished_at")
                       or newest_ok.get("started_at") or "")
        if at:
            age_s = (now - at).total_seconds()
    stale = age_s is None or age_s > stale_after_s
    failing = bool(newest) and newest.get("verdict") == "failed"
    status = "failing" if failing else ("stale" if stale else "ok")
    return {
        "tier": tier,
        "status": status,
        "age_seconds": age_s,
        "stale_after_seconds": stale_after_s,
        "last_success_key": newest_ok.get("key") if newest_ok else None,
        "last_success_at": (newest_ok.get("finished_at")
                            or newest_ok.get("started_at")) if newest_ok else None,
        "last_run": newest,
        "offsite": newest_ok.get("offsite") if newest_ok else "unknown",
        "total_bytes": newest_ok.get("total_bytes", 0) if newest_ok else 0,
    }


def destinations(runs: list[dict], config: dict,
                 credentials_status: str | None = None) -> dict:
    """Where backups actually go — from run evidence plus config.

    ``credentials_status`` is injected by the route (it consults the
    vault); pure callers/tests pass it explicitly.
    """
    newest = max((r for r in runs if r.get("backup_root")),
                 key=_run_sort_key, default=None)
    repo_rows = [r for r in runs if r.get("offsite_repo_bytes")]
    newest_repo = max(repo_rows, key=_run_sort_key, default=None)
    provider = (config.get("offsite_provider") or "").strip()
    bucket = (config.get("offsite_bucket") or "").strip()
    return {
        "local_root": newest.get("backup_root") if newest else None,
        "data_root": newest.get("data_root") if newest else None,
        "provider": provider or None,
        "bucket": bucket or None,
        "repository": (f"rclone:{provider}:{bucket}/restic"
                       if provider and bucket else None),
        "credentials": credentials_status,
        "repo_bytes": (newest_repo.get("offsite_repo_bytes")
                       if newest_repo else None),
        "vault_rows": ["backup.restic-password", "backup.b2-key-id",
                       "backup.b2-application-key"],
    }


def summarize(runs: list[dict], drills: list[dict], config: dict,
              now: datetime | None = None) -> dict:
    """The whole page's answer, in the page's order: am I safe now,
    when was the last good copy, does restore actually work."""
    tiers = [tier_health(runs, config, tier, now=now) for tier in TIERS]
    finished = [d for d in drills if d.get("verdict") != "running"]
    finished.sort(key=lambda d: d.get("key", ""), reverse=True)
    last_drill = finished[0] if finished else None
    running = next((d for d in sorted(drills, key=lambda d: d.get("key", ""),
                                      reverse=True)
                    if d.get("verdict") == "running"), None)
    worst = "ok"
    for health in tiers:
        if health["status"] == "failing":
            worst = "failing"
            break
        if health["status"] == "stale":
            worst = "stale"
    if last_drill and last_drill.get("verdict") != "pass" and worst == "ok":
        worst = "stale"
    return {
        "overall": worst,
        "tiers": tiers,
        "last_drill": last_drill,
        "running_drill": running,
        "config": config,
    }


async def get_summary(request: Request) -> JSONResponse:
    runs = _rows(RUN_SET_ID)
    config = _read_config()
    summary = summarize(runs, _rows(DRILL_SET_ID), config)
    try:
        from tools.dashboard.plugins.backup import credentials
        _, cred_status = credentials.offsite_env(config)
    except Exception:
        cred_status = None
    summary["destinations"] = destinations(runs, config, cred_status)
    return JSONResponse(summary)


async def get_runs(request: Request) -> JSONResponse:
    tier = request.query_params.get("tier")
    if tier and tier not in TIERS:
        return JSONResponse({"error": f"unknown tier {tier!r}"},
                            status_code=400)
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", "50"))))
    except ValueError:
        return JSONResponse({"error": "limit must be an integer"},
                            status_code=400)
    runs = _rows(RUN_SET_ID)
    if tier:
        runs = [r for r in runs if r.get("key", "").startswith(f"{tier}:")]
    runs.sort(key=_run_sort_key, reverse=True)
    return JSONResponse({"runs": runs[:limit]})


async def get_drills(request: Request) -> JSONResponse:
    try:
        limit = max(1, min(100, int(request.query_params.get("limit", "20"))))
    except ValueError:
        return JSONResponse({"error": "limit must be an integer"},
                            status_code=400)
    drills = _rows(DRILL_SET_ID)
    drills.sort(key=lambda d: d.get("key", ""), reverse=True)
    return JSONResponse({"drills": drills[:limit]})


async def get_config(request: Request) -> JSONResponse:
    """Config values plus their schema descriptions, so the page can
    explain every field instead of dumping variable names."""
    fields = {}
    for name, meta in (BackupConfigV1._field_metadata or {}).items():
        fields[name] = {
            "description": meta.get("description", ""),
            "type": meta.get("type", "string"),
        }
        if meta.get("enum"):
            fields[name]["enum"] = meta["enum"]
    return JSONResponse({
        "config": _read_config(),
        "fields": fields,
        "editable": principal_from_request(request).global_authority,
    })


async def put_config(request: Request) -> JSONResponse:
    principal = principal_from_request(request)
    if not principal.global_authority:
        return JSONResponse(
            {"error": "backup configuration requires operator authority"},
            status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"},
                            status_code=400)
    merged = _read_config()
    unknown = sorted(set(body) - set(_config_defaults()))
    if unknown:
        return JSONResponse({"error": f"unknown config fields: {unknown}"},
                            status_code=400)
    merged.update(body)
    from tools.graph import settings_ops
    try:
        settings_ops.add_setting(
            CONFIG_SET_ID, SCHEMA_REVISION, CONFIG_KEY, merged, org=MACHINE)
    except SchemaValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "config": merged})


async def post_reconcile(request: Request) -> JSONResponse:
    """Explicit refresh: ingest the per-tier report files (on the data
    volume) into run rows — bounded worker-thread reads, and the only
    route that touches the filesystem at all. Operator authority."""
    principal = principal_from_request(request)
    if not principal.global_authority:
        return JSONResponse(
            {"error": "backup reconcile requires operator authority"},
            status_code=403)
    from tools.dashboard.plugins.backup import reconcile as reconcile_mod
    try:
        result = reconcile_mod.reconcile()
    except Exception:
        return JSONResponse({"error": "reconcile failed"}, status_code=500)
    return JSONResponse(result)


_drill_task = None  # keeps the fire-and-forget drill task referenced


async def post_drill(request: Request) -> JSONResponse:
    """Start an on-demand restore drill. Operator authority: a drill
    restores multi-GB snapshots and holds the restic repo. A second
    POST while one runs returns the in-flight stamp instead of
    queueing."""
    global _drill_task
    principal = principal_from_request(request)
    if not principal.global_authority:
        return JSONResponse(
            {"error": "restore drills require operator authority"},
            status_code=403)
    import asyncio

    from tools.dashboard.plugins.backup import drill as drill_mod
    in_flight = drill_mod.running_stamp()
    if in_flight:
        return JSONResponse({"running": in_flight}, status_code=409)

    async def _run() -> None:
        try:
            await asyncio.to_thread(drill_mod.run_drill, "manual")
        except drill_mod.DrillAlreadyRunning:
            pass
        except Exception:
            import logging
            logging.getLogger(__name__).exception("on-demand drill failed")

    _drill_task = asyncio.create_task(_run(), name="backup-drill:manual")
    return JSONResponse({"started": True})


routes: list = [
    Route("/api/backup/summary", get_summary, methods=["GET"]),
    Route("/api/backup/reconcile", post_reconcile, methods=["POST"]),
    Route("/api/backup/drill", post_drill, methods=["POST"]),
    Route("/api/backup/runs", get_runs, methods=["GET"]),
    Route("/api/backup/drills", get_drills, methods=["GET"]),
    Route("/api/backup/config", get_config, methods=["GET"]),
    Route("/api/backup/config", put_config, methods=["PUT"]),
]
