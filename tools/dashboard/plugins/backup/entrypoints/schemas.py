"""Setting schemas owned by the ``backup`` plugin.

Everything the plugin renders is settings-backed: the configuration
singleton, one row per capture run, one row per restore drill. The
capture engine's ground truth stays on the backup destination
(``.backup-complete`` + ``run-report.json``, bead auto-yj2wa); these
rows are the bounded projection the dashboard reads, so no request
handler ever touches the (possibly NFS-hung) backup root — decision
driver S3/S7 in graph://7c45a180-345.

Designed per How to Write a Setting (graph://d72e25ec-a41):
SCOPE — a backup describes THIS machine's volume: where its data root
is, what was captured here, whether restore works here. Carried to a
second machine every row would be false, so all three sets are
machine-homed and never publish (band raw..raw).
CARDINALITY — config is a singleton; runs and drills are one row per
event. KEY — runs key by ``<tier>:<stamp>`` (the capture directory's
own name, the handle the reconciler and the operator both hold);
drills key by their start stamp. Key segments are not repeated in the
payload. Trails: the store upserts rows whole, so a drill's per-check
outcomes are an explicit ``checks`` array written at finalization.
"""
from __future__ import annotations

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
    singleton,
)

CONFIG_SET_ID = "backup.config"
RUN_SET_ID = "backup.run"
DRILL_SET_ID = "backup.drill"
SCHEMA_REVISION = 1

TIERS = ("hourly", "daily")
RUN_VERDICTS = ("complete", "failed")
OFFSITE_VERDICTS = ("complete", "failed", "skipped", "unknown")
DRILL_VERDICTS = ("running", "pass", "fail", "timeout")
ORIGINS = ("host", "node")

_STORE_ELEMENT = {
    "name": {"type": "string", "required": True,
             "description": "Store name relative to the capture dir "
                            "(orgs/autonomy.db, tls.key, beads/auto.sql)"},
    "action": {"type": "string",
               "description": "How it was captured: sqlite | copy | "
                              "verify | dump"},
    "status": {"type": "string", "required": True,
               "description": "ok | missing | error | absent-optional"},
    "bytes": {"type": "integer",
              "description": "Captured size; 0 for verify-only stores"},
    "reason": {"type": "string",
               "description": "Failure reason when status is not ok"},
}

_CHECK_ELEMENT = {
    "name": {"type": "string", "required": True,
             "description": "Check name (integrity, sources-sanity, "
                            "beads-count, marker)"},
    "status": {"type": "string", "required": True,
               "description": "ok | fail | skipped"},
    "detail": {"type": "string",
               "description": "What was exercised and what was seen"},
}


@publication_band(min="raw", max="raw")
@home("machine")
@singleton()
class BackupConfigV1(SettingSchema):
    """The machine's backup policy. One row; the background loop and the
    reconciler read it, the /backup page edits it. Offsite credentials
    are deliberately NOT here (custody decision, bead auto-uy896)."""

    set_id = CONFIG_SET_ID
    schema_revision = SCHEMA_REVISION

    hourly_interval_minutes: int = field(
        default=60,
        description="Expected cadence of the hourly tier; staleness is "
                    "judged against this, whoever owns the schedule")
    daily_hour: int = field(
        default=3,
        description="Hour (local, 0-23) the daily tier is expected")
    staleness_multiple: float = field(
        default=3.0,
        description="A tier is stale when age(newest successful capture) "
                    "exceeds this multiple of its expected interval "
                    "(driver S2: the invariant, never scheduler activity)")
    keep_hourly: int = field(
        default=10,
        description="Capture directories retained on the hourly tier")
    keep_daily: int = field(
        default=7,
        description="Capture directories retained on the daily tier")
    drill_cadence_days: float = field(
        default=7.0,
        description="Scheduled restore-drill cadence; 0 disables "
                    "scheduled drills (on-demand still works)")
    drill_timeout_minutes: int = field(
        default=30,
        description="A drill running longer is killed and recorded "
                    "as timeout")
    offsite_enabled: bool = field(
        default=True,
        description="Whether captures push to the offsite restic repo")
    schedule_owner: str = field(
        default="cron", enum=["cron", "plugin"],
        description="Who triggers captures on this machine: the host "
                    "crontab, or the plugin's background loop "
                    "(stakeholder decision 3, graph://7c45a180-345)")
    run_retention: int = field(
        default=50,
        description="backup.run rows kept per tier by the reconciler; "
                    "Settings must stay bounded")
    drill_retention: int = field(
        default=25,
        description="backup.drill rows kept")

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)
        if not 0 <= int(payload.get("daily_hour", 3)) <= 23:
            raise SchemaValidationError("daily_hour must be 0-23")
        for bound in ("hourly_interval_minutes", "keep_hourly", "keep_daily",
                      "drill_timeout_minutes", "run_retention",
                      "drill_retention"):
            if int(payload.get(bound, 1)) < 1:
                raise SchemaValidationError(f"{bound} must be at least 1")
        if float(payload.get("staleness_multiple", 3.0)) < 1.0:
            raise SchemaValidationError(
                "staleness_multiple below 1 would alert on a tier that "
                "is exactly on schedule")
        if float(payload.get("drill_cadence_days", 7.0)) < 0:
            raise SchemaValidationError("drill_cadence_days cannot be negative")


@publication_band(min="raw", max="raw")
@home("machine")
@keyed_per_entity(key_strategy="tier:stamp")
class BackupRunV1(SettingSchema):
    """One capture run. Key: ``<tier>:<stamp>`` — the capture
    directory's own name (``hourly:20260906-040812``), so a row and its
    on-disk evidence name each other. Upserted whole by the reconciler
    from the engine's run-report.json; never hand-written."""

    set_id = RUN_SET_ID
    schema_revision = SCHEMA_REVISION

    verdict: str = field(
        required=True, enum=list(RUN_VERDICTS),
        description="complete = every required store captured; failed = "
                    "any required store missing or errored (the engine's "
                    "exit-code contract, never softened here)")
    started_at: str = field(
        required=True,
        description="ISO-8601 start of the capture")
    finished_at: str = field(
        default="",
        description="ISO-8601 end of the capture")
    duration_seconds: float = field(
        default=0.0,
        description="Wall-clock capture duration")
    origin: str = field(
        default="host", enum=list(ORIGINS),
        description="Which engine captured: host cron/bash or the "
                    "node's rolling mode")
    data_root: str = field(
        default="",
        description="The AUTONOMY_DATA_ROOT the run resolved (the "
                    "2026-09-06 incident was exactly this being wrong)")
    stores: list = field(
        default_factory=list, element=_STORE_ELEMENT,
        description="Per-store outcomes, in capture order")
    store_count: int = field(
        default=0,
        description="Stores captured (marker's stores= line)")
    beads_databases: int = field(
        default=0,
        description="Dolt databases dumped (marker's beads_databases=)")
    total_bytes: int = field(
        default=0,
        description="Sum of captured store bytes")
    offsite: str = field(
        default="unknown", enum=list(OFFSITE_VERDICTS),
        description="Outcome of the offsite push for this run; skipped "
                    "= not configured, unknown = report predates the "
                    "offsite step or the run failed before it")
    failures: list = field(
        default_factory=list, element=str,
        description="Failure reasons, verbatim from the engine")
    exit_code: int = field(
        default=0,
        description="Engine exit code: 0 complete, 1 capture failed, "
                    "2 capture ok but offsite failed")

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)
        # Dict-shaped element specs are descriptive metadata to the
        # substrate; the row shape is enforced here because these rows
        # are machine-written (the reconciler) and a malformed one is a
        # reconciler bug to surface, not tolerate.
        for index, row in enumerate(payload.get("stores") or []):
            if not isinstance(row, dict) or not row.get("name") \
                    or not row.get("status"):
                raise SchemaValidationError(
                    f"stores[{index}] must carry name and status")
        verdict = payload.get("verdict")
        if verdict == "complete" and payload.get("failures"):
            raise SchemaValidationError(
                "a complete run cannot carry failure reasons — the "
                "silent-success defect this plugin exists to prevent")
        if verdict == "failed" and not payload.get("failures"):
            raise SchemaValidationError(
                "a failed run must say why (at least one failure reason)")


@publication_band(min="raw", max="raw")
@home("machine")
@keyed_per_entity(key_strategy="stamp")
class BackupDrillV1(SettingSchema):
    """One restore drill. Key: the drill's start stamp
    (``20260906-051500``). A row is upserted ``running`` when the drill
    starts and finalized whole when it ends, so an in-flight drill is
    visible and a crashed one is diagnosable."""

    set_id = DRILL_SET_ID
    schema_revision = SCHEMA_REVISION

    verdict: str = field(
        required=True, enum=list(DRILL_VERDICTS),
        description="running while in flight; pass only when every "
                    "check is ok; timeout when the subprocess was killed")
    trigger: str = field(
        default="manual", enum=["manual", "scheduled"],
        description="Operator-initiated or cadence-initiated")
    started_at: str = field(
        required=True,
        description="ISO-8601 start")
    finished_at: str = field(
        default="",
        description="ISO-8601 end; empty while running")
    duration_seconds: float = field(
        default=0.0,
        description="Wall-clock drill duration")
    snapshot_id: str = field(
        default="",
        description="Restic snapshot id the drill restored")
    checks: list = field(
        default_factory=list, element=_CHECK_ELEMENT,
        description="Per-check outcomes with what-was-seen detail")
    evidence: str = field(
        default="",
        description="Bounded tail of the drill output (the proof, "
                    "not the full log)")

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)
        verdict = payload.get("verdict")
        checks = payload.get("checks") or []
        for index, check in enumerate(checks):
            if not isinstance(check, dict) or not check.get("name") \
                    or not check.get("status"):
                raise SchemaValidationError(
                    f"checks[{index}] must carry name and status")
        if verdict == "pass":
            if not checks:
                raise SchemaValidationError(
                    "a passing drill must record what it checked")
            bad = [c["name"] for c in checks if c.get("status") == "fail"]
            if bad:
                raise SchemaValidationError(
                    f"a passing drill cannot carry failed checks: {bad}")
        if verdict == "running" and payload.get("finished_at"):
            raise SchemaValidationError(
                "a running drill has no finish time")
