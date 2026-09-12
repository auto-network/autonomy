"""Direct API delivery for graph share; stdout is not the transport."""
import asyncio
import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock

from starlette.requests import Request


def test_share_registers_existing_upload_record(monkeypatch):
    from tools.dashboard import server
    monkeypatch.setattr(server.auth_db, "resolve_token", lambda _: ("auto-share", "autonomy"))
    add = Mock(return_value="attachment-id")
    monkeypatch.setattr(server.graph_ops, "add_setting", add)
    body = dict(rel_path=".attachments/test/image.png", filename="image.png",
                mime="image/png", size=7, alt="Screen", caption="A caption")
    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}
    req = Request({"type": "http", "method": "POST", "path": "/api/session/share",
                   "headers": [(b"authorization", b"Bearer test")]}, receive)
    response = asyncio.run(server.api_session_share(req))
    assert response.status_code == 201
    args, kwargs = add.call_args
    assert args[0] == "dashboard.session.upload"
    assert kwargs["org"] == "machine"
    assert args[3] == {**body, "target_session": "auto-share",
                       "timestamp": args[3]["timestamp"]}
    assert args[3]["timestamp"]


def test_cli_posts_with_stdout_discarded(tmp_path, monkeypatch):
    from tools.graph import cli
    import urllib.request
    monkeypatch.setattr(cli, "_resolve_share_output_root", lambda: tmp_path / "output")
    monkeypatch.setattr(cli, "_resolve_crosstalk_token", lambda: "test-token")
    monkeypatch.setenv("GRAPH_API", "https://dashboard.example")
    poster = Mock(return_value=io.BytesIO(b'{"ok":true}'))
    monkeypatch.setattr(urllib.request, "urlopen", poster)
    file = tmp_path / "image.png"
    file.write_bytes(b"example")
    with redirect_stdout(io.StringIO()) as output:
        cli.cmd_share(SimpleNamespace(file=str(file), alt="Screen", caption="Caption"))
    request = poster.call_args.args[0]
    assert request.full_url == "https://dashboard.example/api/session/share"
    assert request.get_header("Authorization") == "Bearer test-token"
    body = json.loads(request.data)
    assert body["alt"] == "Screen" and body["caption"] == "Caption"
    assert (tmp_path / "output" / body["rel_path"]).read_bytes() == b"example"
    assert "viewer_attachment" not in output.getvalue()
    assert "type" not in body
