"""Jira Cloud REST client — the trusted half of the ``issue_tracker`` broker.

Auth is HTTP basic (account email + API token). The token is read from a host
file and lives only in this process's memory: it is never placed on argv (host
process tables are world-readable), never mounted or exported into an agent
container, and never included in results or errors.

Every rich-text value written (comments, descriptions, textarea custom fields)
is converted markdown -> ADF first: Jira Cloud requires an ADF document even
for custom fields whose editmeta schema claims ``string``/textarea — a plain
string is rejected with 400 "Operation value must be an Atlassian Document".
Custom-field ids are discovered per issue via editmeta, never hardcoded (ids
vary per instance/project).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    def from_env(cls) -> "JiraConfig":
        """Resolve from the dashboard host's environment:
        JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN_FILE (default ~/.jira_token)."""
        base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        email = os.environ.get("JIRA_EMAIL", "")
        token_file = Path(os.environ.get("JIRA_TOKEN_FILE",
                                         str(Path.home() / ".jira_token")))
        token = ""
        try:
            token = token_file.read_text().strip()
        except OSError:
            pass
        missing = [name for name, val in
                   [("JIRA_BASE_URL", base_url), ("JIRA_EMAIL", email),
                    (f"token file {token_file}", token)] if not val]
        if missing:
            raise JiraError(f"jira broker is not configured: missing {', '.join(missing)}")
        return cls(base_url=base_url, email=email, token=token)


def _client(cfg: JiraConfig) -> httpx.Client:
    return httpx.Client(
        base_url=cfg.base_url,
        auth=(cfg.email, cfg.token),
        headers={"Content-Type": "application/json"},
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


def read_ticket(cfg: JiraConfig, key: str) -> dict[str, Any]:
    """The cleaned ticket (ADF already converted to markdown)."""
    with _client(cfg) as c:
        resp = c.get(f"/rest/api/3/issue/{key}")
        _check(resp, f"read {key}")
        return adf.process_ticket(resp.json())


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


def editmeta_field_id(cfg: JiraConfig, key: str, field_name: str) -> str:
    """Discover a field id (e.g. Confirm Plan -> customfield_10153) from the
    issue's editmeta. Ids are per-instance/project — always discovered."""
    with _client(cfg) as c:
        resp = c.get(f"/rest/api/3/issue/{key}/editmeta")
        _check(resp, f"editmeta {key}")
        fields = resp.json().get("fields", {})
    for field_id, meta in fields.items():
        if isinstance(meta, dict) and meta.get("name") == field_name:
            return field_id
    raise JiraError(f"field {field_name!r} is not editable on {key} "
                    f"(not present in editmeta)")


def add_comment(cfg: JiraConfig, key: str, body_markdown: str) -> dict[str, Any]:
    with _client(cfg) as c:
        resp = c.post(f"/rest/api/3/issue/{key}/comment",
                      json={"body": adf.markdown_to_adf(body_markdown)})
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
                     json={"fields": {field_id: adf.markdown_to_adf(body_markdown)}})
        _check(resp, f"set {field_id} on {key}")
    return {"field_id": field_id}


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


def probe(cfg: JiraConfig) -> dict[str, Any]:
    """Cheap auth/reachability check for the capability probe."""
    with _client(cfg) as c:
        resp = c.get("/rest/api/3/myself")
        _check(resp, "probe")
        who = resp.json()
    return {"ok": True, "account": who.get("displayName")}
