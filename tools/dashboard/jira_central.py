"""Settings-native ``jira_write``: one Jira write, reviewed, performed once.

A session's jira-* tool asks for one write (``{"kind": "jira_write",
"request": {op, ...}}`` to /api/approvals; the bridge routes the claimed kind
here). The operator reviews it in the Central inbox and decides with
operator-session authority; the decision is ``{}`` (auto-fkhq0.9, design
checkpoint 2026-09-28).

- Organization: :func:`approval_org.settle_org` -- an organization session
  acts only in its own; a local session names one as ``org_slug``. The
  write runs against that frozen org.
- Content (a comment or field body, the create fields, attachment bytes) is
  unbounded, so it is staged on the accepting machine at creation
  (``data/jira-staging/<approval id>``, quota-bounded) and Central carries its
  sha256. The review carries the text whole when it fits (``complete``) and a
  prefix otherwise; the whole content is served only by the accepting
  machine (:meth:`JiraWriteDesk.content`), sha256-verified on every read.
- Execution happens once, on the accepting machine, within
  :data:`EXECUTE_WINDOW_SECONDS` of the Grant: the machine-homed journal row
  (``autonomy.machine.jira-write``) is claimed before the Jira call. A claim
  left by a stopped process is settled per op: value-setting ops are
  re-applied once (``reapplied_after_restart``; this can overwrite a human
  edit made in between), a transition is reconciled against the issue's
  status, a comment/create is found again by the ``autonomy.approval``
  property it was tagged with, and an attachment becomes ``unknown``.
- Decline, cancel and expiry never touch Jira.

Mirrors mailbox_central.py (planner, attention projection, HTTP adapter,
coordinator).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from agents.capabilities.jira.backend import api
from tools.dashboard.approval_http_bridge import (
    ApprovalHttpBridgeError,
    ApprovalHttpKindAdapter,
    CanonicalLegacyDecision,
)
from tools.dashboard.approval_kind_registry import (
    ApprovalKindRuntime,
    ApprovalPlanningContext,
    ApprovalRequestPlan,
)
from tools.dashboard.approval_org import settle_org
from tools.dashboard.approval_service import ApprovalService, ApprovalServiceError, ApprovalStatus
from tools.dashboard.attention_index_service import AttentionIndexError
from tools.dashboard.attention_registry import (
    AttentionProjectionPlan,
    AttentionPublicationRuntime,
    AttentionSourceEvidence,
)
from tools.dashboard.dashboard_access_central import (
    DashboardAccessCoordinator,
    _bounded_approval_id,
    _opaque_digest,
)
from tools.dashboard.mailbox_central import _requesting_session
from tools.dashboard.vault_open_central import this_machine_label
from tools.graph import settings_ops
from tools.graph.schemas.central_attention import APPROVAL_REQUEST_SET_ID, ApprovalRequestV1
from tools.graph.schemas.jira_write import JIRA_WRITE_REVISION, JIRA_WRITE_SET_ID

logger = logging.getLogger(__name__)

KIND = "jira_write"
APPLICATION_SCOPE = "jira"
RENDERER_ID = "approval.jira_write.review"
CONSUMER_ID = "jira_write.local_execute.v1"
#: A Grant is executed only this long after the resolution.
EXECUTE_WINDOW_SECONDS = 1800
MAX_CONTENT_BYTES = 10 * 1024 * 1024
#: Staged content awaiting a decision or its window, per requester and in all.
MAX_STAGED_BYTES_PER_REQUESTER = 100 * 1024 * 1024
MAX_STAGED_BYTES_TOTAL = 250 * 1024 * 1024
#: ApprovalRequestV1's own bound on safe_review, measured the same way.
_SAFE_REVIEW_MAX_BYTES = 8192
#: The Jira property a comment or created issue is tagged with.
TAG_PROPERTY = "autonomy.approval"
_DESTINATION_DOMAIN = b"dashboard.jira.write-destination.v1"
_ATTENTION_DOMAIN = "dashboard.attention.jira-write-recipient"
_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-[0-9]+$")
_IMAGE_MAGIC = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}

#: Operator-facing states (review.application_result.state).
PENDING = "pending"
AWAITING = "awaiting_execution"
DONE = "done"
FAILED = "failed"
UNKNOWN = "unknown"
EXPIRED = "expired_unexecuted"
ELSEWHERE = "elsewhere"

#: Ops whose re-application after an interrupted claim sets the same value.
_VALUE_OPS = frozenset({"set_field", "set_story_points", "change_type"})
_TITLES = {
    "comment": ("Jira comment", "Post comment"),
    "set_field": ("Jira field update", "Set field"),
    "create": ("New Jira ticket", "Create ticket"),
    "attach": ("Jira attachment", "Attach file"),
    "transition": ("Jira transition", "Transition"),
    "change_type": ("Jira issue-type change", "Change type"),
    "set_story_points": ("Set story points", "Set story points"),
}


class JiraWriteError(RuntimeError):
    """A bounded refusal on the content route. ``code`` is the public message."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def result_destination_id(secret: bytes | None = None) -> str:
    """This Dashboard's opaque identity as the machine that writes."""
    if secret is None:
        from tools.dashboard import unlock_routes
        secret = unlock_routes._session_secret()
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("Dashboard session secret is unavailable")
    digest = hmac.new(secret, _DESTINATION_DOMAIN, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def jira_write_attention_id(approval_id: str) -> str:
    _bounded_approval_id(approval_id)
    return "attention-" + _opaque_digest([_ATTENTION_DOMAIN, 1, approval_id])


# ── staging ──────────────────────────────────────────────────────────


def _staging_root() -> Path:
    from tools.data_paths import DATA_ROOT
    return Path(DATA_ROOT) / "jira-staging"


class Staging:
    """Machine-local staged content, one file per approval, sha256-verified."""

    #: The quota check and the write are one step, so concurrent creates
    #: cannot overshoot it together.
    _lock = threading.Lock()

    def __init__(self, root: Callable[[], Path] = _staging_root):
        self._root = root

    def _paths(self, approval_id: str) -> tuple[Path, Path]:
        _bounded_approval_id(approval_id)
        root = self._root()
        return root / f"{approval_id}.bin", root / f"{approval_id}.json"

    def _metas(self) -> list[dict]:
        root = self._root()
        out = []
        for path in root.glob("*.json") if root.is_dir() else ():
            try:
                meta = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict):
                out.append(meta)
        return out

    def put(self, approval_id: str, requester: str, data: bytes) -> str:
        if len(data) > MAX_CONTENT_BYTES:
            raise ValueError(f"the content is over {MAX_CONTENT_BYTES // (1024 * 1024)} MB")
        with self._lock:
            return self._put(approval_id, requester, data)

    def _put(self, approval_id: str, requester: str, data: bytes) -> str:
        metas = self._metas()
        total = sum(int(m.get("size") or 0) for m in metas)
        mine = sum(int(m.get("size") or 0) for m in metas if m.get("requester") == requester)
        if mine + len(data) > MAX_STAGED_BYTES_PER_REQUESTER:
            raise ValueError("this session has too much Jira content awaiting decisions; "
                             "wait for those to be decided")
        if total + len(data) > MAX_STAGED_BYTES_TOTAL:
            raise ValueError("too much Jira content is awaiting decisions on this machine")
        digest = hashlib.sha256(data).hexdigest()
        blob, meta = self._paths(approval_id)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(data)
        meta.write_text(json.dumps({"approval_id": approval_id, "requester": requester,
                                    "size": len(data), "sha256": digest,
                                    "created_at": time.time()}))
        return digest

    def get(self, approval_id: str, sha256: str) -> bytes:
        """The staged bytes, only if they still hash to the frozen sha256."""
        blob, _meta = self._paths(approval_id)
        try:
            data = blob.read_bytes()
        except OSError:
            raise JiraWriteError("content_missing") from None
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), str(sha256)):
            raise JiraWriteError("content_mismatch")
        return data

    def approval_ids(self) -> list[str]:
        return [str(m["approval_id"]) for m in self._metas() if m.get("approval_id")]

    def remove(self, approval_id: str) -> None:
        for path in self._paths(approval_id):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


# ── planning ─────────────────────────────────────────────────────────


def _text(value: Any, name: str, maximum: int = 400) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > maximum or _TEXT_CONTROL_RE.search(text) or "\n" in text:
        raise ValueError(f"{name} is required text")
    return text


def _key(value: Any) -> str:
    key = _text(value, "key", 64)
    if not _KEY_RE.fullmatch(key):
        raise ValueError("key must be a Jira issue key such as ABC-123")
    return key


def _lines(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    if _TEXT_CONTROL_RE.search(text):
        raise ValueError("the text contains control characters")
    return text.rstrip("\n").split("\n")


def _fits(review: dict) -> bool:
    encoded = json.dumps(review, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return len(encoded) <= _SAFE_REVIEW_MAX_BYTES and all(
        len(line) <= 4096 for line in (review.get("content") or {}).get("lines") or [])


def _fact(label: str, value: Any) -> dict:
    text = str(value if value is not None else "")
    text = _TEXT_CONTROL_RE.sub(" ", text.replace("\n", " "))
    return {"label": label[:80], "value": text[:500]}


def plan_write(body: Mapping[str, Any]) -> tuple[dict, dict, bytes | None, str | None]:
    """Validate one write: ``(request scalars, review facts/labels, content
    bytes or None, review text or None)``. Refuses what could never run."""
    op = body.get("op")
    if op not in _TITLES:
        raise ValueError(f"unknown jira_write op: {op!r}")
    fields = dict(body)
    fields.pop("op")

    def only(*names, optional=()):
        extra = set(fields) - set(names) - set(optional)
        missing = set(names) - set(fields)
        if extra or missing:
            raise ValueError(f"{op} carries {', '.join(names)}"
                             + (f" (optional {', '.join(optional)})" if optional else "")
                             + f"; got {', '.join(sorted(fields))}")

    request: dict[str, Any] = {"op": op}
    facts: list[dict] = []
    content: bytes | None = None
    text: str | None = None
    title, action = _TITLES[op]
    if op == "comment":
        only("key", "body_markdown")
        request["key"] = _key(fields["key"])
        text = str(fields["body_markdown"] or "")
    elif op == "set_field":
        ref = [n for n in ("field_id", "field_name") if n in fields]
        if len(ref) != 1:
            raise ValueError("set_field names exactly one of field_id or field_name")
        only("key", "body_markdown", ref[0])
        request["key"] = _key(fields["key"])
        request[ref[0]] = _text(fields[ref[0]], ref[0], 200)
        facts.append(_fact("Field", request[ref[0]]))
        action = "Set " + request[ref[0]]
        text = str(fields["body_markdown"] or "")
    elif op == "create":
        only("fields")
        create = fields["fields"]
        if not isinstance(create, dict) or not create:
            raise ValueError("create carries a fields object")
        project = (create.get("project") or {}).get("key") if isinstance(
            create.get("project"), dict) else None
        summary = create.get("summary")
        request["project"] = _text(project, "fields.project.key", 64)
        request["summary"] = _text(summary, "fields.summary", 400)
        for name, value in create.items():
            if name != "description":
                facts.append(_fact(name, json.dumps(value) if not isinstance(value, str) else value))
        description = create.get("description")
        text = description if isinstance(description, str) else None
        content = json.dumps(create, sort_keys=True).encode("utf-8")
    elif op == "attach":
        only("key", "filename", "mime_type", "content_b64", optional=("size",))
        request["key"] = _key(fields["key"])
        request["filename"] = _text(os.path.basename(str(fields["filename"] or "")), "filename", 200)
        request["media_type"] = _text(fields["mime_type"], "mime_type", 100).lower()
        try:
            content = base64.b64decode(str(fields["content_b64"] or ""), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("content_b64 is not base64") from None
        facts += [_fact("File", request["filename"]),
                  _fact("Size", f"{len(content) / 1024:.1f} KB"),
                  _fact("Type", request["media_type"])]
    elif op == "transition":
        only("key", "transition", optional=("fields", "to_status"))
        request["key"] = _key(fields["key"])
        request["transition"] = _text(fields["transition"], "transition", 200)
        if fields.get("to_status"):
            request["to_status"] = _text(fields["to_status"], "to_status", 200)
        extra = fields.get("fields") or {}
        if not isinstance(extra, dict) or not all(
                isinstance(k, str) and isinstance(v, (str, int, float)) for k, v in extra.items()):
            raise ValueError("transition fields map names to values")
        request["fields"] = {k: str(v) for k, v in extra.items()}
        facts.append(_fact("Transition", request["transition"]))
        facts += [_fact(k, v) for k, v in request["fields"].items()]
        action = "Transition to " + request["transition"]
    elif op == "change_type":
        only("key", "issue_type")
        request["key"] = _key(fields["key"])
        request["issue_type"] = _text(fields["issue_type"], "issue_type", 200)
        facts.append(_fact("Issue type", request["issue_type"]))
        action = "Change type to " + request["issue_type"]
    elif op == "set_story_points":
        only("key", "value", "board_id", optional=("previous_value", "field_id"))
        request["key"] = _key(fields["key"])
        request["value"] = _text(str(fields["value"]), "value", 40)
        request["board_id"] = fields["board_id"]
        if not isinstance(request["board_id"], (int, str)) or isinstance(request["board_id"], bool):
            raise ValueError("board_id is the board's id")
        previous = fields.get("previous_value")
        facts += [_fact("Current", "Unestimated" if previous in (None, "") else previous),
                  _fact("New", request["value"])]
        title = action = f"Set {request['value']} story points"
    if text is not None:
        if content is None:
            content = text.encode("utf-8")
        if not text.strip() and op != "create":
            raise ValueError("the text is empty")
    labels = {"title": title, "action_label": action, "facts": facts}
    return request, labels, content, text


def build_request_planner(
    *,
    destination_resolver: Callable[[], str] = result_destination_id,
    machine_label: Callable[[], str] = this_machine_label,
    staging: Staging | None = None,
):
    # No Jira config check here: resolving it reads the Jira token from the
    # vault, and planning never touches a credential. The tools' read
    # preflights already exercise the org's Jira configuration.
    store = staging or Staging()

    def plan(context: ApprovalPlanningContext, body: Mapping[str, Any]) -> ApprovalRequestPlan:
        if not isinstance(body, Mapping):
            raise ValueError("a jira_write request is an object")
        session = _requesting_session(context)
        org, body = settle_org(context, body)
        request, labels, content, text = plan_write(body)
        destination = destination_resolver()
        if not isinstance(destination, str) or len(destination) != 43:
            raise ValueError("this Dashboard cannot write to Jira right now")
        machine = machine_label()
        label = context.requester_ref.get("label") or session
        target = request.get("key") or request.get("project") or ""
        # The operation's own fields, as the design states name them.
        shown = {k: request[k] for k in ("project", "summary", "issue_type", "transition",
                                         "to_status", "value", "filename") if k in request}
        if request["op"] == "set_field":
            shown["field"] = request.get("field_name") or request.get("field_id")
        review = {
            **labels,
            **shown,
            "detail": f"{label} wants to: {labels['action_label']} ({target}, {org}).",
            "requester_label": label,
            "machine_label": machine,
            "op": request["op"],
            "org": org,
            "target": target,
        }
        if content is not None:
            digest = store.put(context.approval_id, str(context.requester_ref.get("id")), content)
            meta = {"sha256": digest, "size": len(content),
                    "media_type": request.get("media_type") or "text/markdown"}
            if request["op"] == "attach":
                meta["filename"] = request["filename"]
            request["content"] = meta
            # Complete when the review holds everything there is to see: an
            # attachment is always fetched; a create without a description is
            # its (untruncated) facts.
            whole = request["op"] == "create" and not any(
                len(f["value"]) >= 500 for f in labels["facts"])
            review["content"] = {**meta, "complete": whole and not text, "lines": []}
            if text:
                lines = _lines(text)
                review["content"].update(complete=True, lines=lines)
                while not _fits(review) and review["content"]["lines"]:
                    kept = review["content"]["lines"]
                    review["content"].update(complete=False,
                                             lines=[line[:2000] for line in kept[:len(kept) // 2]])
        if not _fits(review):
            raise ValueError("the request is too large to review")
        return ApprovalRequestPlan(
            subject_ref=f"jira-write:{context.approval_id}",
            safe_review=review,
            request={**request, "org": org},
            staged={"result_destination_id": destination, "machine_label": machine},
        )

    return plan


def _validate_decision(_context, _request, decision, _is_grant) -> dict[str, Any]:
    if decision:
        raise ValueError("jira_write decisions carry no payload")
    return {}


def build_approval_runtime(**planner_options) -> ApprovalKindRuntime:
    return ApprovalKindRuntime(
        request_planner=build_request_planner(**planner_options),
        decision_validator=_validate_decision,
        resolution_consumer_id=CONSUMER_ID,
        result_ref_builder=lambda approval_id, _request, _decision: f"jira-write:{approval_id}",
    )


def build_attention_runtime(approvals: ApprovalService) -> AttentionPublicationRuntime:
    def plan(source: Any) -> AttentionProjectionPlan:
        if not isinstance(source, ApprovalStatus):
            raise ValueError("jira_write projection requires approval status")
        request = source.request.payload
        if request.get("kind") != KIND:
            raise ValueError("jira_write projection kind mismatch")
        resolution = source.resolution
        review = request.get("safe_review") or {}
        return AttentionProjectionPlan(
            attention_id=jira_write_attention_id(source.request.approval_id),
            object_ref=source.request.approval_id,
            participant_role="recipient",
            attention_state="resolved" if resolution is not None else "needs_attention",
            safe_title=str(review.get("title") or "Jira write")[:120],
            safe_summary=(f"{review.get('requester_label') or 'A session'}: "
                          f"{review.get('action_label') or '?'} ({review.get('target') or '?'})")[:240],
            counterparty_ref=None,
            occurred_at=(float(resolution.payload["resolved_at"]) if resolution is not None
                         else float(request["created_at"])),
            source_version=2 if resolution is not None else 1,
        )

    def evidence(object_ref: str, source_version: int) -> AttentionSourceEvidence:
        status = approvals.status(_bounded_approval_id(object_ref))
        actual = 2 if status.resolution is not None else 1
        if actual != source_version or status.request.payload.get("kind") != KIND:
            raise AttentionIndexError("stale_source")
        return AttentionSourceEvidence(
            source_guard={"kind": "approval", "ref": object_ref, "version": source_version},
            source_expires_at=status.request.payload.get("expires_at"),
        )

    return AttentionPublicationRuntime(projection_planner=plan, source_evidence_builder=evidence)


# ── execution ────────────────────────────────────────────────────────


def _journal(approval_id: str) -> dict | None:
    row = settings_ops.read_set_key(JIRA_WRITE_SET_ID, approval_id, org="machine", peers=[])
    payload = (row or {}).get("payload")
    return dict(payload) if isinstance(payload, dict) else None


def _record(approval_id: str, payload: dict) -> None:
    settings_ops.write_by_key(JIRA_WRITE_SET_ID, JIRA_WRITE_REVISION, approval_id,
                              {k: v for k, v in payload.items() if v is not None},
                              org="machine")


class JiraWriteDesk:
    """The accepting machine's once-only execution of a granted write."""

    _lock = threading.Lock()
    # Writes running in this process; a "claimed" row not in flight here was
    # left by a stopped process.
    _inflight: set = set()

    def __init__(
        self,
        *,
        approvals: ApprovalService,
        index: Any = None,
        destination_resolver: Callable[[], str] = result_destination_id,
        staging: Staging | None = None,
        jira: Any = api,
        config: Callable[[str], Any] = lambda org: api.JiraConfig.resolve(org=org),
        journal: Callable[[str], dict | None] = _journal,
        record: Callable[[str, dict], None] = _record,
        owner: Callable[[], tuple[int, str | None]] | None = None,
        owner_alive: Callable[[Mapping[str, Any]], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        from tools.dashboard import mcp_crosstalk_central as liveness
        self._owner = owner or liveness._owner
        self._owner_alive = owner_alive or liveness._owner_alive
        self.approvals = approvals
        self.index = index
        self._destination_resolver = destination_resolver
        self.staging = staging or Staging()
        self._jira = jira
        self._config = config
        self._journal = journal
        self._record = record
        self._clock = clock

    def _here(self, payload: Mapping[str, Any]) -> bool:
        staged = payload.get("staged")
        destination = staged.get("result_destination_id") if isinstance(staged, Mapping) else None
        try:
            ours = self._destination_resolver()
        except Exception:
            return False
        return isinstance(destination, str) and hmac.compare_digest(destination, ours)

    def _window_open(self, status: ApprovalStatus) -> bool:
        resolved = float(status.resolution.payload["resolved_at"])
        return self._clock() < resolved + EXECUTE_WINDOW_SECONDS

    def state(self, status: ApprovalStatus) -> tuple[str, dict]:
        payload = status.request.payload
        if payload.get("kind") != KIND:
            raise ValueError("not a jira_write approval")
        here = self._here(payload)
        resolution = status.resolution
        if resolution is None:
            return (PENDING if here else ELSEWHERE), {}
        if resolution.payload.get("outcome") != "granted":
            return str(resolution.payload.get("outcome")), {}
        if not here:
            return ELSEWHERE, {}
        row = self._journal(status.request.approval_id)
        if row is not None and row.get("state") in (DONE, FAILED, UNKNOWN):
            return row["state"], row
        if row is None and not self._window_open(status):
            return EXPIRED, {}
        return AWAITING, row or {}

    def operator_result(self, status: ApprovalStatus) -> dict:
        state, row = self.state(status)
        result: dict[str, Any] = {
            "state": state,
            "machine_label": (status.request.payload.get("staged") or {}).get("machine_label") or "",
        }
        if row.get("error"):
            result["reason"] = str(row["error"])[:500]
        if state == DONE and row.get("result"):
            result["result"] = json.loads(row["result"])
        if row.get("reapplied_after_restart"):
            result["reapplied_after_restart"] = True
        return result

    def requester_result(self, status: ApprovalStatus) -> dict | None:
        state, row = self.state(status)
        if state == DONE:
            outcome = json.loads(row.get("result") or "{}")
            if row.get("reapplied_after_restart"):
                op = (status.request.payload.get("request") or {}).get("op")
                outcome["note"] = (
                    "re-applied after an interrupted run; it may have overwritten an "
                    "edit made in between" if op in _VALUE_OPS
                    else "applied after an interrupted run found it had not landed")
            return {"approved": True, "execution": {"ok": True, **outcome}}
        if state == FAILED:
            return {"approved": True, "execution": {"ok": False, "error": row.get("error")}}
        if state == UNKNOWN:
            return {"approved": True, "execution": {"ok": False, "error": row.get("error")}}
        if state == EXPIRED:
            return {"approved": True, "execution": {
                "ok": False, "error": "approved too late; nothing was sent to Jira. Ask again."}}
        return None

    # ── the write ──

    def _perform(self, approval_id: str, request: Mapping[str, Any], cfg: Any) -> dict:
        op = request["op"]
        jira = self._jira
        tag = {TAG_PROPERTY: approval_id}
        content = request.get("content")
        data = self.staging.get(approval_id, content["sha256"]) if content else None
        if op == "comment":
            return jira.add_comment(cfg, request["key"], data.decode("utf-8"), tag=tag)
        if op == "set_field":
            return jira.set_editable_field(cfg, request["key"],
                                           request.get("field_id") or request["field_name"],
                                           data.decode("utf-8"))
        if op == "create":
            return jira.create_issue(cfg, json.loads(data), tag=tag)
        if op == "attach":
            return jira.add_attachment(cfg, request["key"], request["filename"], data,
                                       request["media_type"])
        if op == "transition":
            return jira.transition_issue(cfg, request["key"], request["transition"],
                                         request.get("fields") or None)
        if op == "change_type":
            return jira.change_issue_type(cfg, request["key"], request["issue_type"])
        if op == "set_story_points":
            return jira.set_story_points(cfg, request["key"], request["value"],
                                         request.get("board_id"))
        raise JiraWriteError("unknown_op")

    def _reconcile(self, approval_id: str, request: Mapping[str, Any], cfg: Any,
                   claim: Mapping[str, Any] | None = None) -> dict | None:
        """After an interrupted claim: the result if the write already landed."""
        op = request["op"]
        if op == "comment":
            return self._jira.find_comment(cfg, request["key"], TAG_PROPERTY, approval_id)
        if op == "create":
            return self._jira.find_created_issue(cfg, request["project"], request["summary"],
                                                 TAG_PROPERTY, approval_id)
        if op == "transition":
            # Already in the destination status recorded before the call: it
            # landed. Otherwise apply once; a transition Jira no longer offers
            # from here fails, never loops.
            target = (claim or {}).get("target_status")
            status = self._jira.issue_status(cfg, request["key"])
            if target and status.casefold() == target.casefold():
                return {"transition": request["transition"], "to_status": status,
                        "reconciled": True}
        return None

    def _transition_target(self, cfg: Any, request: Mapping[str, Any]) -> str | None:
        """The destination status of the named transition, as Jira offers it now."""
        wanted = request["transition"].casefold()
        for option in self._jira.list_transitions(cfg, request["key"]):
            if (option.get("name") or "").casefold() == wanted \
                    or (option.get("to_status") or "").casefold() == wanted:
                return option.get("to_status") or None
        return None

    def materialize(self, status: ApprovalStatus) -> bool:
        payload = status.request.payload
        resolution = status.resolution
        if payload.get("kind") != KIND or resolution is None \
                or resolution.payload.get("outcome") != "granted" or not self._here(payload):
            return False
        approval_id = status.request.approval_id
        request = payload["request"]
        reapply = False
        # The claim below is check-then-write under an in-process lock: the
        # once-only guarantee assumes one dashboard process per machine (use a
        # compare-and-set write here if the machine journal ever gains one).
        with self._lock:
            if approval_id in self._inflight:
                return True
            existing = self._journal(approval_id)
            if existing is not None and existing.get("state") != "claimed":
                return True
            if existing is None and not self._window_open(status):
                return False
            if existing is not None:
                if self._owner_alive(existing):
                    # Another process holds this claim and is still running:
                    # it is in the middle of the call, not interrupted.
                    return True
                # The process that claimed it is gone.
                if existing.get("reapplied_after_restart") or request["op"] == "attach" \
                        or not self._window_open(status):
                    self._record(approval_id, {**existing, "state": UNKNOWN,
                                               "finished_at": self._clock(),
                                               "owner_pid": None, "owner_start": None,
                                               "error": "the write was interrupted; it may or may "
                                                        "not have been applied and was not retried"})
                    return True
                reapply = True
            pid, start = self._owner()
            claim = {"state": "claimed",
                     "claimed_at": (existing or {}).get("claimed_at", self._clock()),
                     "reapplied_after_restart": True if reapply else None,
                     "owner_pid": pid, "owner_start": start,
                     "target_status": (existing or {}).get("target_status")}
            self._record(approval_id, {k: v for k, v in claim.items() if v is not None})
            self._inflight.add(approval_id)
        outcome: dict[str, Any]
        try:
            cfg = self._config(request["org"])
            found = self._reconcile(approval_id, request, cfg, existing) if reapply else None
            if found is None and request["op"] == "transition" and not claim.get("target_status"):
                claim["target_status"] = self._transition_target(cfg, request)
                self._record(approval_id, {k: v for k, v in claim.items() if v is not None})
            reviewed = request.get("to_status")
            if found is None and request["op"] == "transition" and reviewed and (
                    (claim.get("target_status") or "").casefold() != reviewed.casefold()):
                # What was reviewed is what runs: the transition no longer
                # leads where the operator approved it to.
                raise JiraWriteError(
                    f"this transition now leads to {claim.get('target_status') or 'nothing'}, "
                    f"not the reviewed {reviewed}")
            result = found if found is not None else self._perform(approval_id, request, cfg)
            outcome = {"state": DONE, "result": json.dumps(result, default=str)[:4000]}
            if found is not None:
                outcome["reapplied_after_restart"] = None
        except JiraWriteError as exc:
            outcome = {"state": FAILED, "error": (
                f"the staged content is unusable ({exc.code})"
                if exc.code in ("content_missing", "content_mismatch", "unknown_op") else exc.code)}
        except api.JiraError as exc:
            outcome = {"state": FAILED, "error": str(exc)[:1000]}
        except Exception as exc:  # never leave a claim without an outcome
            logger.warning("jira_write: %s failed (%s)", approval_id, type(exc).__name__)
            outcome = {"state": FAILED, "error": type(exc).__name__}
        with self._lock:
            try:
                claim = self._journal(approval_id) or {}
                merged = {**claim, **outcome, "finished_at": self._clock(),
                          "owner_pid": None, "owner_start": None}
                self._record(approval_id, {k: v for k, v in merged.items() if v is not None})
            finally:
                self._inflight.discard(approval_id)
        return True

    def prune(self) -> None:
        """Forget staged content once its approval is terminal and past its window."""
        now = self._clock()
        for approval_id in self.staging.approval_ids():
            try:
                status = self.approvals.status(approval_id)
            except (ApprovalServiceError, ValueError) as exc:
                if getattr(exc, "code", "") == "not_found":
                    self.staging.remove(approval_id)
                continue
            resolution = status.resolution
            if resolution is None:
                continue
            if now >= float(resolution.payload["resolved_at"]) + EXECUTE_WINDOW_SECONDS \
                    and approval_id not in self._inflight:
                self.staging.remove(approval_id)

    # ── the operator's content route ──

    def content(self, attention_id: Any) -> tuple[str, Any, dict]:
        """``("json", {"lines": [...]}, headers)`` for text or ``("bytes", data,
        headers)`` for an attachment, served only by the accepting machine and
        only when the staged bytes still match the frozen sha256."""
        if self.index is None:
            raise JiraWriteError("not_found")
        try:
            item = self.index.get_query_item(attention_id)
        except ValueError:
            raise JiraWriteError("not_found") from None
        if item is None:
            raise JiraWriteError("not_found")
        try:
            status = self.approvals.status(_bounded_approval_id(item.payload.get("object_ref")))
        except (ApprovalServiceError, ValueError):
            raise JiraWriteError("not_found") from None
        payload = status.request.payload
        if payload.get("kind") != KIND or \
                item.attention_id != jira_write_attention_id(status.request.approval_id):
            raise JiraWriteError("not_found")
        if not self._here(payload):
            raise JiraWriteError("elsewhere")
        request = payload["request"]
        meta = request.get("content")
        if not meta:
            raise JiraWriteError("not_found")
        data = self.staging.get(status.request.approval_id, meta["sha256"])
        secure = {"X-Content-Type-Options": "nosniff",
                  "Content-Security-Policy": "sandbox; default-src 'none'",
                  "Cache-Control": "no-store"}
        if request["op"] != "attach":
            text = data.decode("utf-8")
            if request["op"] == "create":
                text = json.loads(text).get("description") or ""
            return "json", {"lines": _lines(text)}, secure
        media = meta.get("media_type") or ""
        magic = _IMAGE_MAGIC.get(media)
        if magic and any(data.startswith(m) for m in magic) and (
                media != "image/webp" or data[8:12] == b"WEBP"):
            return "bytes", data, {**secure, "Content-Type": media,
                                   "Content-Disposition": "inline"}
        name = re.sub(r"[^A-Za-z0-9._-]", "_", str(meta.get("filename") or "attachment"))[:120]
        return "bytes", data, {**secure, "Content-Type": "application/octet-stream",
                               "Content-Disposition": f'attachment; filename="{name}"'}


def build_http_adapter(
    desk: JiraWriteDesk,
    *,
    reconcile: Callable[[str], ApprovalStatus | None] | None = None,
) -> ApprovalHttpKindAdapter:
    def project_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = payload.get("request")
        if not isinstance(request, Mapping):
            raise RuntimeError("jira_write request is unavailable")
        return {k: request[k] for k in ("op", "key", "org") if k in request}

    def map_decision(body: Mapping[str, Any]) -> CanonicalLegacyDecision:
        if body == {"approved": False}:
            return CanonicalLegacyDecision("declined", {})
        raise ApprovalHttpBridgeError("invalid_decision")

    def project_result(status: ApprovalStatus) -> Mapping[str, Any] | None:
        if reconcile is not None:
            refreshed = reconcile(status.request.approval_id)
            if refreshed is not None:
                status = refreshed
        return desk.requester_result(status)

    return ApprovalHttpKindAdapter(kind=KIND, request_projector=project_request,
                                   result_projector=project_result,
                                   legacy_decision_mapper=map_decision)


class JiraWriteCoordinator(DashboardAccessCoordinator):
    """The dashboard-access wake coordinator for ``jira_write``: publishes the
    attention item and executes a Grant on the accepting machine."""

    def __init__(self, *, desk: JiraWriteDesk, **kwargs) -> None:
        super().__init__(consumer=None, **kwargs)
        self.desk = desk

    def reconcile_exact(self, approval_id: str) -> ApprovalStatus | None:
        try:
            status = self.approvals.status(_bounded_approval_id(approval_id))
        except ApprovalServiceError as exc:
            if exc.code == "not_found":
                return None
            raise
        if status.request.payload.get("kind") != KIND:
            return None
        self.index.publish(self.producer, status)
        if status.resolution is not None:
            self.desk.materialize(status)
        return status

    def _scan_ids(self) -> tuple[str, ...]:
        self.desk.prune()
        rows = settings_ops.read_set(APPROVAL_REQUEST_SET_ID, org=None, peers=[])
        if any(rows.dropped.values()):
            raise RuntimeError("partial Central approval request read")
        selected = []
        for row in rows:
            if not isinstance(row.payload, dict):
                raise RuntimeError("invalid Central approval request row")
            ApprovalRequestV1.validate(row.payload)
            if row.payload.get("kind") == KIND:
                selected.append(_bounded_approval_id(row.key))
        return tuple(sorted(set(selected)))


__all__ = [
    "APPLICATION_SCOPE", "CONSUMER_ID", "EXECUTE_WINDOW_SECONDS", "KIND", "RENDERER_ID",
    "JiraWriteCoordinator", "JiraWriteDesk", "JiraWriteError", "Staging",
    "build_approval_runtime", "build_attention_runtime", "build_http_adapter",
    "jira_write_attention_id", "plan_write", "result_destination_id",
]
