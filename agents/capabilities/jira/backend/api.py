"""Jira Cloud REST client — the trusted half of the ``issue_tracker`` broker.

Auth is HTTP basic (account email + API token). The token is read from a host
file and lives only in this process's memory: it is never placed on argv (host
process tables are world-readable), never mounted or exported into an agent
container, and never included in results or errors.

Every rich-text value written (comments, descriptions, textarea custom fields)
is converted markdown -> ADF first: Jira Cloud requires an ADF document even
for custom fields whose editmeta schema claims ``string``/textarea — a plain
string is rejected with 400 "Operation value must be an Atlassian Document".
Custom-field ids are discovered from Jira metadata, never hardcoded (ids vary
per instance/project). Existing-ticket fields use editmeta; board estimation
uses field metadata plus the Jira Software board configuration.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

import httpx

from agents.capabilities.jira.backend import adf

# Test seam: when set, clients are built on this transport instead of the
# network (httpx.MockTransport in tests).
_transport: httpx.BaseTransport | None = None


class JiraError(Exception):
    """A Jira API call failed. The message never contains credentials."""


@dataclass(frozen=True)
class JiraConfig:
    base_url: str
    email: str
    token: str

    @classmethod
    def resolve(cls, org: str | None = None) -> "JiraConfig":
        """Resolve broker config. Non-secret values (base URL, account email,
        token-file PATH) come from the org install Setting
        ``autonomy.org.capability.install#1`` key=``issue_tracker`` in *org*'s
        database — the designed home for org capability config; no literal
        secret is ever in the graph. Environment variables (JIRA_BASE_URL,
        JIRA_EMAIL, JIRA_TOKEN_FILE) override per value — the test seam. The
        token itself is read from the host file and exists only in process
        memory."""
        installed: dict = {}
        # No org named means there is no organization's configuration to
        # read. Asking anyway resolved to the operator's own store, where
        # capability-install rows do not live, so the read found nothing
        # and the failure looked identical to "not configured" -- sending
        # a reader to fix a Setting that was correct all along. The env
        # overrides below are the deliberate path when no org is named.
        if org:
            try:
                from tools.graph import ops as graph_ops
                members = graph_ops.read_set("autonomy.org.capability.install",
                                             org=org, peers=[])
                for m in (getattr(members, "members", []) or []):
                    payload = m.payload if isinstance(m.payload, dict) else {}
                    if payload.get("contract") == "issue_tracker":
                        installed = payload.get("broker_config") or {}
                        break
            except Exception:
                pass
        base_url = (os.environ.get("JIRA_BASE_URL")
                    or installed.get("base_url", "")).rstrip("/")
        email = os.environ.get("JIRA_EMAIL") or installed.get("email", "")
        # The API token is the operator's PERSONAL Atlassian credential, sealed
        # in their personal AUDITED vault under an org-writeback key
        # (``<org>:jira_token``) — never in the org's shared store, and a read
        # scoped to this org can only ever see this org's own slot. Audited
        # releases inline/unattended: ``read_set`` opens the value in-process
        # when the delegate is warm, or returns a ``vault_error`` when the vault
        # is locked (fail closed — never fall through to plaintext). The env
        # vars stay as explicit test/override seams; the old implicit
        # ``~/.jira_token`` default is gone (it never survived a recreate).
        token = os.environ.get("JIRA_TOKEN") or ""
        env_file = os.environ.get("JIRA_TOKEN_FILE")
        if not token and env_file:
            try:
                token = Path(env_file).expanduser().read_text().strip()
            except OSError:
                pass
        vault_locked = False
        if not token and org:
            try:
                from tools.graph import settings_ops
                from tools.graph.schemas.vault_credential import (
                    VAULT_AUDITED_SET_ID,
                )
                row = settings_ops.read_set_key(
                    VAULT_AUDITED_SET_ID, f"{org}:jira_token",
                    org=org, peers=[],
                )
            except Exception:
                row = None
            if row is not None and row.get("vault_error") is None:
                # The vault stores exact bytes; a token file sealed with a
                # trailing newline would otherwise reach Jira as "<token>\n"
                # and 401. Tokens are single-line text — strip surrounding
                # whitespace, exactly as the old token_file path did.
                token = ((row.get("payload") or {}).get("value", "") or "").strip()
            elif row is not None:
                vault_locked = True
        if not token and installed.get("token_file"):
            try:
                token = Path(installed["token_file"]).expanduser().read_text().strip()
            except OSError:
                pass
        missing = [name for name, val in
                   [("base_url", base_url), ("email", email),
                    ("token", token)] if not val]
        if missing:
            if "token" in missing and vault_locked:
                where = (f"the Jira token is sealed at {org}:jira_token but the "
                         "vault is locked — unlock the vault once to release it")
            elif "token" in missing and org:
                where = (f"seal it: graph vault seal jira_token --org {org} "
                         "--tier audited --from-file <path>; set base_url/email "
                         "on the issue_tracker org install Setting")
            elif org:
                where = ("set base_url/email on the issue_tracker org install "
                         f"Setting in {org!r}")
            else:
                where = ("no org was named — name one, or set JIRA_BASE_URL / "
                         "JIRA_EMAIL / JIRA_TOKEN[_FILE]")
            raise JiraError(
                f"jira broker is not configured: missing "
                f"{', '.join(missing)} ({where})")
        return cls(base_url=base_url, email=email, token=token)


def _client(cfg: JiraConfig) -> httpx.Client:
    # No default Content-Type: httpx sets application/json for json= bodies
    # and the multipart boundary for files= bodies (attachment upload).
    return httpx.Client(
        base_url=cfg.base_url,
        auth=(cfg.email, cfg.token),
        timeout=30.0,
        transport=_transport,
    )


def _check(resp: httpx.Response, what: str) -> None:
    if resp.status_code >= 300:
        detail = ""
        try:
            body = resp.json()
            msgs = (body.get("errorMessages") or []) + [
                f"{k}: {v}" for k, v in (body.get("errors") or {}).items()]
            detail = "; ".join(str(m) for m in msgs)
        except Exception:
            detail = resp.text[:500]
        raise JiraError(f"{what} failed (HTTP {resp.status_code}): {detail}")


def story_points_field(cfg: JiraConfig) -> dict[str, Any]:
    """Return this Jira site's classic ``Story Points`` numeric field.

    Jira Cloud can expose both the company-managed ``Story Points`` field and
    the team-managed ``Story point estimate`` field.  Their ids are allocated
    per site, so neither may be hardcoded.  Prefer the exact classic field
    name (and its untranslated equivalent) and reject ambiguity instead of
    silently reporting the other estimation field.
    """
    with _client(cfg) as c:
        resp = c.get(
            "/rest/api/3/field/search",
            params={"type": "custom", "query": "Story Points", "maxResults": 100},
        )
        _check(resp, "discover Story Points field")
        fields = resp.json().get("values") or []

    def is_classic(field: dict[str, Any]) -> bool:
        schema = field.get("schema") or {}
        names = (field.get("name"), field.get("untranslatedName"))
        clauses = field.get("clauseNames") or []
        return schema.get("type") == "number" and (
            any(
                str(name or "").strip().casefold() == "story points"
                for name in names
            )
            or any(
                str(clause or "").strip().casefold() in {
                    "story points", "story points[number]"
                }
                for clause in clauses
            )
        )

    matches = [
        field for field in fields
        if isinstance(field, dict) and is_classic(field)
    ]
    if len(matches) == 1 and matches[0].get("id"):
        return matches[0]
    if not matches:
        available = ", ".join(
            f"{field.get('name')} ({field.get('id')})"
            for field in fields if isinstance(field, dict)
        ) or "none"
        raise JiraError(
            "Jira has no unambiguous numeric field named 'Story Points'. "
            f"Matching fields returned by Jira: {available}"
        )
    choices = ", ".join(
        f"{field.get('name')} ({field.get('id')})" for field in matches
    )
    raise JiraError(f"Jira has multiple fields named 'Story Points': {choices}")


def read_ticket(cfg: JiraConfig, key: str) -> dict[str, Any]:
    """The cleaned ticket (ADF already converted to markdown)."""
    points_field_id = story_points_field(cfg)["id"]
    with _client(cfg) as c:
        resp = c.get(f"/rest/api/3/issue/{key}")
        _check(resp, f"read {key}")
        return adf.process_ticket(resp.json(), story_points_field_id=points_field_id)


# Terse row fields for search results. Sprint remains an instance-local field
# for the current installation; Story Points is discovered per Jira site.
_SEARCH_FIELDS = [
    "summary", "status", "assignee", "priority", "fixVersions", "updated",
    "customfield_10020",   # sprint
]


def search_issues(cfg: JiraConfig, jql: str, max_results: int = 50,
                  page_token: str | None = None) -> dict[str, Any]:
    """Run a JQL search and return terse cleaned rows.

    Uses ``POST /rest/api/3/search/jql`` — the current search endpoint
    (the legacy ``/rest/api/3/search`` startAt-pagination API was removed
    by Atlassian in 2025). Pagination is by opaque ``nextPageToken``:
    ``next_page_token`` in the result is ``None`` on the last page,
    otherwise pass it back in as *page_token* for the next page.
    """
    points_field_id = story_points_field(cfg)["id"]
    payload: dict[str, Any] = {
        "jql": jql,
        "maxResults": max(1, min(int(max_results), 100)),
        "fields": [*_SEARCH_FIELDS, points_field_id],
    }
    if page_token:
        payload["nextPageToken"] = page_token
    with _client(cfg) as c:
        resp = c.post("/rest/api/3/search/jql", json=payload)
        _check(resp, "search")
        body = resp.json()
    items: list[dict[str, Any]] = []
    for issue in body.get("issues") or []:
        f = issue.get("fields") or {}
        sprint_data = f.get("customfield_10020")
        items.append({
            "key": issue.get("key"),
            "summary": f.get("summary"),
            "status": (f.get("status") or {}).get("name"),
            "priority": (f.get("priority") or {}).get("name"),
            "assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
            "fix_versions": [v.get("name") for v in (f.get("fixVersions") or [])],
            "sprint": [s.get("name") for s in sprint_data
                       if isinstance(s, dict)] if isinstance(sprint_data, list) else [],
            "story_points": f.get(points_field_id),
            "updated": f.get("updated"),
        })
    return {"items": items, "next_page_token": body.get("nextPageToken")}


def _issue_project_key(cfg: JiraConfig, key: str) -> str:
    with _client(cfg) as c:
        resp = c.get(f"/rest/api/3/issue/{key}", params={"fields": "project"})
        _check(resp, f"read {key} project")
    project = ((resp.json().get("fields") or {}).get("project") or {}).get("key")
    if not project:
        raise JiraError(f"Jira did not return a project for {key}")
    return str(project)


def _project_boards(cfg: JiraConfig, project: str) -> list[dict[str, Any]]:
    """All visible boards associated with *project*, following pagination."""
    boards: list[dict[str, Any]] = []
    start_at = 0
    with _client(cfg) as c:
        while True:
            resp = c.get(
                "/rest/agile/1.0/board",
                params={
                    "projectKeyOrId": project,
                    "startAt": start_at,
                    "maxResults": 50,
                },
            )
            _check(resp, f"list boards for {project}")
            body = resp.json()
            page = [
                row for row in (body.get("values") or [])
                if isinstance(row, dict)
            ]
            boards.extend(page)
            if body.get("isLast", True) or not page:
                break
            start_at += len(page)
    return boards


def estimation_context(cfg: JiraConfig, key: str,
                       board_id: int | str | None = None) -> dict[str, Any]:
    """Resolve the board and off-screen field used to estimate *key*.

    The Jira Software estimation API requires a board id.  With no explicit
    board, use the issue's project boards and select one configured with the
    site's classic Story Points field. Multiple boards are safe when they all
    target that same field.
    """
    points_field = story_points_field(cfg)
    project = _issue_project_key(cfg, key)
    boards = ([{"id": int(board_id), "name": str(board_id)}]
              if board_id is not None else _project_boards(cfg, project))
    if not boards:
        raise JiraError(f"no visible Jira Software board is associated with {project}")

    candidates: list[dict[str, Any]] = []
    with _client(cfg) as c:
        for board in boards:
            candidate_id = board.get("id")
            if candidate_id is None:
                continue
            resp = c.get(
                f"/rest/agile/1.0/issue/{key}/estimation",
                params={"boardId": candidate_id},
            )
            if resp.status_code >= 300:
                if board_id is not None:
                    _check(resp, f"read {key} estimation for board {candidate_id}")
                continue
            estimation = resp.json()
            if estimation.get("fieldId") == points_field["id"]:
                candidates.append({
                    "board_id": int(candidate_id),
                    "board_name": board.get("name"),
                    "field_id": estimation.get("fieldId"),
                    "field_name": points_field.get("name"),
                    "project": project,
                    "value": estimation.get("value"),
                })

    if not candidates:
        suffix = f" on board {board_id}" if board_id is not None else ""
        raise JiraError(
            f"no Story Points estimation configuration was found for {key}{suffix}"
        )
    return candidates[0]


def set_story_points(cfg: JiraConfig, key: str, value: str,
                     board_id: int | str | None = None) -> dict[str, Any]:
    """Set *key*'s board estimate even when the field is off the edit screen."""
    raw_value = str(value).strip()
    try:
        numeric = Decimal(raw_value)
    except InvalidOperation:
        raise JiraError(f"story points must be a number, got {raw_value!r}")
    if not numeric.is_finite() or numeric < 0:
        raise JiraError(
            "story points must be a finite non-negative number, "
            f"got {raw_value!r}"
        )
    context = estimation_context(cfg, key, board_id=board_id)
    with _client(cfg) as c:
        resp = c.put(
            f"/rest/agile/1.0/issue/{key}/estimation",
            params={"boardId": context["board_id"]},
            json={"value": raw_value},
        )
        _check(resp, f"set Story Points on {key}")
        body = resp.json()
    return {
        "board_id": context["board_id"],
        "field_id": body.get("fieldId") or context["field_id"],
        "value": body.get("value", raw_value),
    }


def createmeta(cfg: JiraConfig, project: str, issuetype: str,
               version_prefix: str | None = None) -> dict[str, Any]:
    """Create-metadata for a project/issuetype: components, versions,
    priorities, and any Severity-style select field, shaped for an agent."""
    with _client(cfg) as c:
        resp = c.get("/rest/api/3/issue/createmeta",
                     params={"projectKeys": project, "issuetypeNames": issuetype,
                             "expand": "projects.issuetypes.fields"})
        _check(resp, f"createmeta {project}/{issuetype}")
        data = resp.json()
    try:
        fields = data["projects"][0]["issuetypes"][0]["fields"]
    except (KeyError, IndexError):
        raise JiraError(f"createmeta: no field metadata for {project}/{issuetype}")

    def _allowed(field: dict | None, name_key: str) -> list[dict]:
        return [{"id": v.get("id"), name_key: v.get(name_key)}
                for v in (field or {}).get("allowedValues", [])]

    versions = [
        {"id": v.get("id"), "name": v.get("name"), "released": v.get("released"),
         "releaseDate": v.get("releaseDate")}
        for v in (fields.get("versions") or {}).get("allowedValues", [])
        if version_prefix is None or (v.get("name") or "").startswith(version_prefix)
    ]
    versions.sort(key=lambda v: v.get("releaseDate") or "0000-00-00", reverse=True)
    released = [v for v in versions if v.get("released")]

    severity_field = next(
        (f for f in fields.values()
         if isinstance(f, dict) and f.get("name") == "Severity"), None)

    return {
        "severity": _allowed(severity_field, "value"),
        "components": _allowed(fields.get("components"), "name"),
        "versions": versions,
        "latest_released_version": (
            {"id": released[0]["id"], "name": released[0]["name"]} if released else None),
        "priorities": _allowed(fields.get("priority"), "name"),
    }


def _allowed_value_label(value: Any) -> str | None:
    """Return the most useful agent-facing label for an allowed value."""
    if not isinstance(value, dict):
        return str(value) if value is not None else None
    for field in ("name", "value", "displayName", "key", "id"):
        candidate = value.get(field)
        if candidate not in (None, ""):
            return str(candidate)
    return None


def list_editable_fields(cfg: JiraConfig, key: str) -> list[dict[str, Any]]:
    """List fields editable on *key*, including schemas and allowed values.

    Jira's editmeta endpoint is the authoritative read-only discovery surface
    for existing-ticket updates.  Exposing it avoids the previous anti-pattern
    of staging an intentionally invalid write merely to receive the valid-field
    list after operator approval.
    """
    with _client(cfg) as c:
        resp = c.get(f"/rest/api/3/issue/{key}/editmeta")
        _check(resp, f"editmeta {key}")
        fields = resp.json().get("fields", {})
    out: list[dict[str, Any]] = []
    for field_id, meta in fields.items():
        if not isinstance(meta, dict):
            continue
        schema = meta.get("schema") or {}
        allowed = [
            label
            for value in (meta.get("allowedValues") or [])
            if (label := _allowed_value_label(value)) is not None
        ]
        out.append({
            "id": field_id,
            "name": meta.get("name") or field_id,
            "required": bool(meta.get("required")),
            "type": schema.get("type"),
            "items": schema.get("items"),
            "allowed": allowed,
        })
    out.sort(key=lambda field: (str(field["name"]).casefold(), field["id"]))
    return out


def editmeta_field(cfg: JiraConfig, key: str,
                   field_reference: str) -> dict[str, Any]:
    """Return an editable field's id, display name, and schema.

    ``field_reference`` may be either the Jira field id or its display name.
    Display-name matching is case-insensitive; ids win on an exact match.
    """
    fields = list_editable_fields(cfg, key)
    by_id = {field["id"]: field for field in fields}
    if field_reference in by_id:
        return by_id[field_reference]
    wanted = field_reference.strip().casefold()
    for field in fields:
        if str(field.get("name") or "").casefold() == wanted:
            return field
    valid = sorted(
        f"{field['name']} ({field['id']})" for field in fields
    )
    raise JiraError(
        f"field {field_reference!r} is invalid for jira-update on {key}. "
        f"Valid fields: {', '.join(valid) or 'none'}"
    )


def editmeta_field_id(cfg: JiraConfig, key: str, field_name: str) -> str:
    """Compatibility wrapper returning only an editable field's Jira id."""
    return editmeta_field(cfg, key, field_name)["id"]


def list_issue_types(cfg: JiraConfig, key: str) -> dict[str, Any]:
    """The issue types valid in *key*'s project, plus its current type."""
    with _client(cfg) as c:
        resp = c.get(f"/rest/api/3/issue/{key}",
                     params={"fields": "issuetype,project"})
        _check(resp, f"read {key}")
        f = resp.json().get("fields") or {}
        current = f.get("issuetype") or {}
        project = (f.get("project") or {}).get("key") or ""
        resp = c.get(f"/rest/api/3/project/{project}")
        _check(resp, f"project {project}")
        types = resp.json().get("issueTypes") or []
    return {
        "project": project,
        "current": {"id": current.get("id"), "name": current.get("name"),
                    "subtask": bool(current.get("subtask"))},
        "issue_types": [{"id": t.get("id"), "name": t.get("name"),
                         "subtask": bool(t.get("subtask"))} for t in types],
    }


def change_issue_type(cfg: JiraConfig, key: str,
                      type_name: str) -> dict[str, Any]:
    """Change *key*'s issue type (the API face of Jira's "Move").

    The edit endpoint only accepts ``{"issuetype": {"id": ...}}`` with the
    project-scoped numeric id — a name string 400s with "Could not find
    issuetype by id or name" — so the target name is resolved against the
    project's type list first. Standard→standard changes go through
    ``PUT /issue``; conversions in or out of sub-task are a hierarchy Move
    the REST API doesn't support, and are rejected with a clear message
    rather than a confusing Jira error."""
    info = list_issue_types(cfg, key)
    want = type_name.strip().casefold()
    match = next((t for t in info["issue_types"]
                  if (t["name"] or "").casefold() == want), None)
    if match is None:
        valid = ", ".join(t["name"] for t in info["issue_types"]
                          if not t["subtask"]) or "none"
        raise JiraError(
            f"no issue type named {type_name!r} in project "
            f"{info['project']}. Valid types: {valid}")
    current = info["current"]
    if match["id"] == current.get("id"):
        raise JiraError(f"{key} is already a {match['name']}")
    if match["subtask"] or current.get("subtask"):
        raise JiraError(
            f"changing {key} between sub-task and standard types "
            f"({current.get('name')} -> {match['name']}) is a hierarchy "
            "Move the Jira REST API does not support — it needs the "
            "operator in the Jira UI")
    with _client(cfg) as c:
        resp = c.put(f"/rest/api/3/issue/{key}",
                     json={"fields": {"issuetype": {"id": match["id"]}}})
        _check(resp, f"change {key} to {match['name']}")
    return {"key": key, "from": current.get("name"), "to": match["name"]}


def _transition_meta(cfg: JiraConfig, key: str) -> list[dict[str, Any]]:
    """Raw transition metadata for *key*: id, name, destination status, and
    each transition-screen field with its required flag and schema.
    ``expand=transitions.fields`` surfaces screen-level validators; workflow
    validators with no screen field only fire on POST (their message comes
    back through :func:`_check`)."""
    with _client(cfg) as c:
        resp = c.get(f"/rest/api/3/issue/{key}/transitions",
                     params={"expand": "transitions.fields"})
        _check(resp, f"transitions for {key}")
        body = resp.json()
    out: list[dict[str, Any]] = []
    for t in body.get("transitions") or []:
        fields = []
        for field_id, meta in (t.get("fields") or {}).items():
            if not isinstance(meta, dict):
                continue
            schema = meta.get("schema") or {}
            fields.append({
                "id": field_id,
                "name": meta.get("name"),
                "required": bool(meta.get("required")),
                "type": schema.get("type"),
                "items": schema.get("items"),
                "allowed": [v.get("name") or v.get("value")
                            for v in meta.get("allowedValues") or []
                            if isinstance(v, dict)],
            })
        out.append({"id": t.get("id"), "name": t.get("name"),
                    "to_status": (t.get("to") or {}).get("name"),
                    "fields": fields})
    return out


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def list_transitions(cfg: JiraConfig, key: str) -> list[dict[str, Any]]:
    """Workflow transitions available on *key* from its current status,
    with each transition's required screen fields annotated ``has_value``
    (whether the issue already satisfies them) so a caller can compute
    what's missing *before* staging a write."""
    meta = _transition_meta(cfg, key)
    required_ids = sorted({f["id"] for t in meta for f in t["fields"]
                           if f["required"]})
    current: dict[str, Any] = {}
    if required_ids:
        with _client(cfg) as c:
            resp = c.get(f"/rest/api/3/issue/{key}",
                         params={"fields": ",".join(required_ids)})
            _check(resp, f"read {key} fields")
            current = resp.json().get("fields") or {}
    return [{
        "id": t["id"], "name": t["name"], "to_status": t["to_status"],
        "required_fields": [
            {"id": f["id"], "name": f["name"], "type": f["type"],
             "has_value": _has_value(current.get(f["id"])),
             "allowed": f["allowed"]}
            for f in t["fields"] if f["required"]],
    } for t in meta]


def _match_transition(transitions: list[dict], transition_name: str,
                      key: str) -> dict:
    """Match case-insensitively against the transition name first, then the
    destination status name (operators think in status names; the two can
    differ per workflow). On no match the error lists what's valid from the
    ticket's current status so the caller can retry instead of guessing."""
    want = transition_name.strip().casefold()
    for probe in ("name", "to_status"):
        match = next((t for t in transitions
                      if (t.get(probe) or "").casefold() == want), None)
        if match is not None:
            return match
    valid = "; ".join(f"{t['name']} -> {t['to_status']}"
                      for t in transitions) or "none"
    raise JiraError(
        f"no transition named {transition_name!r} on {key} from its "
        f"current status. Valid transitions: {valid}")


def _resolve_user(cfg: JiraConfig, query: str) -> str:
    """Resolve a display name / email to an accountId; unique match only."""
    with _client(cfg) as c:
        resp = c.get("/rest/api/3/user/search", params={"query": query})
        _check(resp, f"user search {query!r}")
        users = [u for u in resp.json() if u.get("accountId")]
    if len(users) == 1:
        return users[0]["accountId"]
    names = ", ".join(u.get("displayName", "?") for u in users[:10])
    raise JiraError(
        f"user {query!r} resolves to {len(users)} accounts"
        + (f" ({names})" if names else "")
        + " — use an exact display name or email")


def _coerce_field_value(cfg: JiraConfig, field: dict, value: str) -> Any:
    """Shape a CLI-provided string for the field's schema type. Arrays take
    comma-separated values. Users resolve display name/email -> accountId."""
    ftype, items = field.get("type"), field.get("items")
    if ftype == "array":
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if items == "option":
            return [{"value": p} for p in parts]
        if items == "user":
            return [{"accountId": _resolve_user(cfg, p)} for p in parts]
        if items in ("version", "component"):
            return [{"name": p} for p in parts]
        return parts
    if ftype == "option":
        return {"value": value}
    if ftype == "user":
        return {"accountId": _resolve_user(cfg, value)}
    if ftype in ("version", "component", "priority", "resolution"):
        return {"name": value}
    if ftype == "number":
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            raise JiraError(f"field {field.get('name')!r} expects a number, "
                            f"got {value!r}")
    return value


def transition_issue(cfg: JiraConfig, key: str, transition_name: str,
                     fields: dict[str, str] | None = None) -> dict[str, Any]:
    """Move *key* through the workflow transition named *transition_name*,
    optionally setting transition-screen fields (by display name or id) in
    the same POST — required-field validators mean some transitions only
    succeed with fields supplied alongside them."""
    meta = _transition_meta(cfg, key)
    match = _match_transition(meta, transition_name, key)
    payload: dict[str, Any] = {"transition": {"id": match["id"]}}
    if fields:
        by_name = {(f.get("name") or "").casefold(): f for f in match["fields"]}
        by_id = {f["id"]: f for f in match["fields"]}
        coerced: dict[str, Any] = {}
        for name, value in fields.items():
            field = by_id.get(name) or by_name.get(name.strip().casefold())
            if field is None:
                available = ", ".join(
                    f.get("name") or f["id"] for f in match["fields"]) or "none"
                raise JiraError(
                    f"field {name!r} is not on the {match['name']!r} "
                    f"transition screen for {key}. Available: {available}")
            coerced[field["id"]] = _coerce_field_value(cfg, field, str(value))
        payload["fields"] = coerced
    with _client(cfg) as c:
        resp = c.post(f"/rest/api/3/issue/{key}/transitions", json=payload)
        _check(resp, f"transition {key} -> {match['name']}")
    return {"transition": match["name"], "to_status": match["to_status"],
            "fields_set": sorted((payload.get("fields") or {}).keys())}


# Signed media URLs look like https://api.media.atlassian.com/file/<uuid>/…
_MEDIA_FILE_UUID = re.compile(
    r"/file/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE)


def _media_resolver(cfg: JiraConfig, key: str,
                    targets: list[str]) -> Callable[[str], str | None]:
    """Resolver mapping *key*'s attachment filenames -> media-services file
    UUIDs, for inline (``mediaSingle``) embedding.

    Jira's REST API never returns the media UUID directly; it only appears
    in the signed media URL the attachment-content endpoint 303-redirects
    to, so each referenced attachment costs one unfollowed GET whose
    ``Location`` is parsed. Filenames that aren't attachments (or whose
    redirect doesn't carry a UUID) resolve to ``None`` — the converter then
    leaves the image line as literal text rather than failing the write."""
    with _client(cfg) as c:
        resp = c.get(f"/rest/api/3/issue/{key}",
                     params={"fields": "attachment"})
        _check(resp, f"read {key} attachments")
        rows = (resp.json().get("fields") or {}).get("attachment") or []
        by_name: dict[str, dict] = {}
        for a in rows:
            name = a.get("filename")
            # Duplicate filenames: keep the newest upload.
            if name and (name not in by_name
                         or (a.get("created") or "") > (by_name[name].get("created") or "")):
                by_name[name] = a
        mapping: dict[str, str] = {}
        for target in dict.fromkeys(targets):
            row = by_name.get(target)
            if not row or not row.get("id"):
                continue
            redirect = c.get(f"/rest/api/3/attachment/content/{row['id']}",
                             follow_redirects=False)
            match = _MEDIA_FILE_UUID.search(redirect.headers.get("location", ""))
            if match:
                mapping[target] = match.group(1)
    return mapping.get


def _rich_body(cfg: JiraConfig, key: str, body_markdown: str) -> dict[str, Any]:
    """Markdown -> ADF, embedding block-image references to *key*'s
    attachments as inline media. Resolution only runs when the body
    actually references images."""
    targets = adf.image_targets(body_markdown)
    resolver = _media_resolver(cfg, key, targets) if targets else None
    return adf.markdown_to_adf(body_markdown, media_resolver=resolver)


def add_comment(cfg: JiraConfig, key: str, body_markdown: str) -> dict[str, Any]:
    with _client(cfg) as c:
        resp = c.post(f"/rest/api/3/issue/{key}/comment",
                      json={"body": _rich_body(cfg, key, body_markdown)})
        _check(resp, f"comment on {key}")
        body = resp.json()
    return {"id": body.get("id"),
            "author": (body.get("author") or {}).get("displayName"),
            "created": body.get("created")}


def set_field(cfg: JiraConfig, key: str, field_id: str,
              body_markdown: str) -> dict[str, Any]:
    """Set a rich-text field (ADF, even for editmeta 'string' textareas).
    Success is HTTP 204 with no body."""
    with _client(cfg) as c:
        resp = c.put(f"/rest/api/3/issue/{key}",
                     json={"fields": {field_id: _rich_body(cfg, key, body_markdown)}})
        _check(resp, f"set {field_id} on {key}")
    return {"field_id": field_id}


def set_editable_field(cfg: JiraConfig, key: str, field_reference: str,
                       value: str) -> dict[str, Any]:
    """Set an existing issue field using its editmeta schema.

    The existing ``jira-update`` contract treats string fields as rich text,
    preserving Description/Confirm Plan/textarea behavior. Structured schemas
    reuse the same coercion as transition-screen fields: version/component
    arrays become ``[{"name": ...}]``, users resolve to ``accountId``, options
    become ``{"value": ...}``, and numeric fields become numbers.
    """
    field = editmeta_field(cfg, key, field_reference)
    if field["id"] == "issuetype":
        # An issue-type change is Jira's "Move", not a field edit — the
        # edit path sends a shape Jira rejects. Point at the real op.
        raise JiraError(
            f"changing the issue type of {key} is not a field edit — "
            "use jira-change-type (it resolves the type id and preflights "
            "valid targets)")
    if field.get("type") not in {
        "array", "option", "user", "version", "component",
        "priority", "resolution", "number",
    }:
        return set_field(cfg, key, field["id"], value)

    coerced = _coerce_field_value(cfg, field, value.strip())
    with _client(cfg) as c:
        resp = c.put(
            f"/rest/api/3/issue/{key}",
            json={"fields": {field["id"]: coerced}},
        )
        _check(resp, f"set {field['id']} on {key}")
    return {"field_id": field["id"]}


def create_issue(cfg: JiraConfig, fields: dict[str, Any]) -> dict[str, Any]:
    """Create an issue. A plain-string ``description`` is converted to ADF."""
    fields = dict(fields)
    if isinstance(fields.get("description"), str):
        fields["description"] = adf.markdown_to_adf(fields["description"])
    with _client(cfg) as c:
        resp = c.post("/rest/api/3/issue", json={"fields": fields})
        _check(resp, "create issue")
        body = resp.json()
    return {"key": body.get("key"), "id": body.get("id"),
            "url": f"{cfg.base_url}/browse/{body.get('key')}"}


def get_attachment(cfg: JiraConfig, attachment_id: str) -> tuple[bytes, str, str]:
    """Download attachment content -> (bytes, filename, mime_type).

    Jira's content endpoint 303-redirects to a signed media URL on another
    host; redirects are followed for this call only (the signed URL carries
    its own auth, and httpx drops basic auth on cross-origin hops)."""
    with _client(cfg) as c:
        meta_resp = c.get(f"/rest/api/3/attachment/{attachment_id}")
        _check(meta_resp, f"attachment {attachment_id} metadata")
        meta = meta_resp.json()
        resp = c.get(f"/rest/api/3/attachment/content/{attachment_id}",
                     follow_redirects=True)
        _check(resp, f"attachment {attachment_id} content")
        return (resp.content,
                meta.get("filename") or f"attachment-{attachment_id}",
                meta.get("mimeType") or "application/octet-stream")


def add_attachment(cfg: JiraConfig, key: str, filename: str, content: bytes,
                   mime_type: str = "application/octet-stream") -> dict[str, Any]:
    """Upload an attachment (multipart; Jira requires the XSRF opt-out header)."""
    with _client(cfg) as c:
        resp = c.post(f"/rest/api/3/issue/{key}/attachments",
                      headers={"X-Atlassian-Token": "no-check"},
                      files={"file": (filename, content, mime_type)})
        _check(resp, f"attach {filename} to {key}")
        body = resp.json()
    first = body[0] if isinstance(body, list) and body else {}
    return {"id": first.get("id"), "filename": first.get("filename"),
            "size": first.get("size")}


def probe(cfg: JiraConfig) -> dict[str, Any]:
    """Cheap auth/reachability check for the capability probe."""
    with _client(cfg) as c:
        resp = c.get("/rest/api/3/myself")
        _check(resp, "probe")
        who = resp.json()
    return {"ok": True, "account": who.get("displayName")}
