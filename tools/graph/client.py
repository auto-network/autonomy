"""GraphClient — single dispatch point for graph reads.

CLI commands call ``get_client().method(...)``. The "am I in a container?"
branch lives here in exactly one place: if ``GRAPH_API`` is set we route
HTTP through the dashboard (single-writer + WAL-fresh reads); otherwise we
call ``ops.*`` directly against the local DB.

The HttpClient mirrors the LocalClient interface so call sites are identical
in either mode.

Design reference: graph://bcce359d-a1d (Cross-Org Search Architecture).
"""

from __future__ import annotations

import json as _json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import ops


class GraphClient:
    """Base interface. Methods are implemented on the concrete subclasses."""

    def search(
        self,
        q: str,
        *,
        org: str | None = None,
        peers: list[str] | None = None,
        only_org: str | None = None,
        limit: int = 25,
        project: str | None = None,
        or_mode: bool = False,
        tag: str | None = None,
        states: list[str] | None = None,
        include_raw: bool = False,
        session_source_ids: list[str] | None = None,
        session_author_pattern: str | None = None,
    ) -> list[dict]:
        raise NotImplementedError

    def get_source(
        self,
        source_id: str,
        *,
        org: str | None = None,
        peers: list[str] | None = None,
    ) -> dict | None:
        raise NotImplementedError

    def get_attachment(
        self,
        attachment_id: str,
        *,
        org: str | None = None,
        peers: list[str] | None = None,
    ) -> dict | None:
        raise NotImplementedError

    def list_attachments(
        self,
        source_id: str | None = None,
        *,
        org: str | None = None,
        peers: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict]:
        raise NotImplementedError

    def list_sources(
        self,
        *,
        org: str | None = None,
        peers: list[str] | None = None,
        only_org: str | None = None,
        limit: int = 50,
        project: str | None = None,
        source_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict]:
        raise NotImplementedError

    def list_attention(
        self,
        *,
        org: str | None = None,
        since: str | None = None,
        search: str | None = None,
        last: int | None = None,
        session: str | None = None,
        context: int = 0,
    ) -> list[dict]:
        raise NotImplementedError

    def list_collab_topics(
        self,
        *,
        org: str | None = None,
    ) -> list[dict]:
        raise NotImplementedError

    def list_collab_sources(
        self,
        *,
        org: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        raise NotImplementedError


class LocalClient(GraphClient):
    """Calls ``ops.*`` directly against the local DB. Used on the host."""

    def search(
        self,
        q: str,
        *,
        org: str | None = None,
        peers: list[str] | None = None,
        only_org: str | None = None,
        limit: int = 25,
        project: str | None = None,
        or_mode: bool = False,
        tag: str | None = None,
        states: list[str] | None = None,
        include_raw: bool = False,
        session_source_ids: list[str] | None = None,
        session_author_pattern: str | None = None,
    ) -> list[dict]:
        return ops.search(
            q,
            org=org,
            peers=peers,
            only_org=only_org,
            limit=limit,
            project=project,
            or_mode=or_mode,
            tag=tag,
            states=states,
            include_raw=include_raw,
            session_source_ids=session_source_ids,
            session_author_pattern=session_author_pattern,
        )

    def get_source(self, source_id, *, org=None, peers=None):
        return ops.get_source(source_id, org=org, peers=peers)

    def get_attachment(self, attachment_id, *, org=None, peers=None):
        return ops.get_attachment(attachment_id, org=org, peers=peers)

    def list_attachments(self, source_id=None, *, org=None, peers=None, limit=50):
        return ops.list_attachments(
            source_id=source_id, org=org, peers=peers, limit=limit,
        )

    def list_sources(
        self,
        *,
        org=None,
        peers=None,
        only_org=None,
        limit=50,
        project=None,
        source_type=None,
        tags=None,
    ):
        return ops.list_sources(
            org=org,
            peers=peers,
            only_org=only_org,
            limit=limit,
            project=project,
            source_type=source_type,
            tags=tags,
        )

    def list_attention(
        self,
        *,
        org=None,
        since=None,
        search=None,
        last=None,
        session=None,
        context=0,
    ):
        return ops.list_attention(
            org=org,
            since=since,
            search=search,
            last=last,
            session=session,
            context=context,
        )

    def list_collab_topics(self, *, org=None):
        return ops.list_collab_topics(org=org)

    def list_collab_sources(self, *, org=None, limit=50):
        return ops.list_collab_sources(org=org, limit=limit)


class HttpClient(GraphClient):
    """Routes graph reads through the dashboard API.

    Used in containers where ``GRAPH_API`` is set. Provides WAL-fresh reads
    by talking to the writer process, and is the substrate over which
    cross-org search will run server-side once per-org DB ships.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _get(self, path: str, params: dict | None = None) -> Any:
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            url = f"{self.base_url}{path}?{qs}"
        else:
            url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=30, context=self._ssl_ctx)
            return _json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                err_body = _json.loads(e.read())
                raise GraphHttpError(err_body.get("error", str(e)), e.code)
            except _json.JSONDecodeError:
                raise GraphHttpError(str(e), e.code)
        except urllib.error.URLError as e:
            raise GraphHttpError(f"Cannot reach graph API at {self.base_url}: {e.reason}", 0)

    def search(
        self,
        q: str,
        *,
        org=None,
        peers=None,
        only_org=None,
        limit=25,
        project=None,
        or_mode=False,
        tag=None,
        states=None,
        include_raw=False,
        session_source_ids=None,
        session_author_pattern=None,
    ) -> list[dict]:
        params = {"q": q, "limit": str(limit)}
        if project:
            params["project"] = project
        if or_mode:
            params["or"] = "1"
        if tag:
            params["tag"] = tag
        if states:
            params["states"] = ",".join(states)
        if include_raw:
            params["include_raw"] = "1"
        if only_org:
            params["only_org"] = only_org
        if peers is not None:
            params["peers"] = ",".join(peers)
        if session_source_ids:
            params["session_source_ids"] = ",".join(session_source_ids)
        if session_author_pattern:
            params["session_author_pattern"] = session_author_pattern
        result = self._get("/api/graph/search", params)
        return result if isinstance(result, list) else []

    def get_source(self, source_id, *, org=None, peers=None):
        try:
            return self._get(f"/api/graph/source/{source_id}")
        except GraphHttpError as e:
            if e.status == 404:
                return None
            raise

    def get_attachment(self, attachment_id, *, org=None, peers=None):
        try:
            return self._get(f"/api/graph/attachment/{attachment_id}")
        except GraphHttpError as e:
            if e.status == 404:
                return None
            raise

    def list_attachments(self, source_id=None, *, org=None, peers=None, limit=50):
        if not source_id:
            raise NotImplementedError("HttpClient.list_attachments requires source_id")
        result = self._get(f"/api/source/{source_id}/attachments")
        if isinstance(result, dict) and "attachments" in result:
            return result["attachments"]
        return []

    def list_sources(
        self,
        *,
        org=None,
        peers=None,
        only_org=None,
        limit=50,
        project=None,
        source_type=None,
        tags=None,
    ):
        params: dict[str, str] = {"limit": str(limit)}
        if project:
            params["project"] = project
        if source_type:
            params["type"] = source_type
        if tags:
            params["tags"] = ",".join(tags)
        if only_org:
            params["only_org"] = only_org
        if peers is not None:
            params["peers"] = ",".join(peers)
        result = self._get("/api/graph/sources", params)
        if isinstance(result, dict) and "sources" in result:
            return result["sources"]
        return result if isinstance(result, list) else []

    def list_attention(
        self,
        *,
        org=None,
        since=None,
        search=None,
        last=None,
        session=None,
        context=0,
    ):
        # Server endpoint returns formatted text — not used by container CLI today.
        # Container `graph attention` runs against the local DB; this stub keeps
        # the interface uniform for future cross-org use.
        raise NotImplementedError("HttpClient.list_attention: server endpoint TODO")

    def list_collab_topics(self, *, org=None):
        result = self._get("/api/graph/collab-topics")
        if isinstance(result, dict) and "topics" in result:
            return result["topics"]
        return result if isinstance(result, list) else []

    def list_collab_sources(self, *, org=None, limit=50):
        result = self._get("/api/graph/collab", {"limit": str(limit)})
        if isinstance(result, dict) and "notes" in result:
            return result["notes"]
        return result if isinstance(result, list) else []


class GraphHttpError(Exception):
    """Raised when the dashboard graph API returns an error."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


# ── Dispatcher ──────────────────────────────────────────────


def get_client() -> GraphClient:
    """Return the appropriate GraphClient for the current environment.

    Container (GRAPH_API set) → HttpClient.
    Host                     → LocalClient.
    """
    api = os.environ.get("GRAPH_API")
    if api:
        return HttpClient(api)
    return LocalClient()
