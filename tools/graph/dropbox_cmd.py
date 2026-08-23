"""CLI access to the machine-global operator dropbox."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request


def _request(path: str, token: str):
    base = os.environ.get("GRAPH_API", "https://localhost:8080").rstrip("/")
    request = urllib.request.Request(
        base + path,
        headers={"Authorization": f"Bearer {token}"},
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.urlopen(request, timeout=30, context=context)


def _fail(exc: BaseException) -> None:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            message = json.loads(exc.read()).get("error") or str(exc)
        except Exception:
            message = str(exc)
    elif isinstance(exc, urllib.error.URLError):
        message = f"cannot reach dashboard: {exc.reason}"
    else:
        message = str(exc)
    raise SystemExit(f"Error: {message}")


def _size_text(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def cmd_list(args) -> None:
    try:
        with _request(
            "/api/dropbox?limit=" + urllib.parse.quote(str(args.limit)),
            args._resolve_token(),
        ) as response:
            body = json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
        _fail(exc)
    for row in body.get("items", []):
        name = row.get("original_filename") or "-"
        print(
            f"{row.get('created_at', '?')}  {row.get('id', '?')}  "
            f"{row.get('content_type', '?')}  {_size_text(row.get('size', 0))}  {name}"
        )


def _response_filename(response, item_id: str) -> str:
    header = response.headers.get("Content-Disposition", "")
    encoded = re.search(
        r"filename\*=utf-8''([^;]+)", header, re.IGNORECASE,
    )
    plain = re.search(r'filename="?([^";]+)', header, re.IGNORECASE)
    supplied = urllib.parse.unquote(
        encoded.group(1) if encoded else plain.group(1) if plain else "",
    )
    supplied = supplied.replace("\\", "/").rsplit("/", 1)[-1]
    supplied = re.sub(r"[^\w. -]", "_", supplied).strip(" .")[:160]
    return f"{item_id}-{supplied}" if supplied else f"{item_id}.bin"


def cmd_get(args) -> None:
    try:
        response = _request(
            "/api/dropbox/" + urllib.parse.quote(args.id, safe=""),
            args._resolve_token(),
        )
        with response:
            item_id = response.headers.get("X-Autonomy-Dropbox-Id") or args.id
            output_dir = Path(args.output_dir).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            target = output_dir / _response_filename(response, item_id)
            if target.exists():
                stem, suffix = target.stem, target.suffix
                counter = 1
                while target.exists():
                    target = output_dir / f"{stem}-{counter}{suffix}"
                    counter += 1
            fd, tmp_name = tempfile.mkstemp(prefix=".dropbox-", dir=output_dir)
            try:
                digest = hashlib.sha256()
                with os.fdopen(fd, "wb") as fh:
                    while chunk := response.read(1024 * 1024):
                        fh.write(chunk)
                        digest.update(chunk)
                    fh.flush()
                    os.fsync(fh.fileno())
                expected = response.headers.get("X-Autonomy-SHA256")
                if expected and digest.hexdigest() != expected:
                    raise OSError("download hash does not match dropbox receipt")
                os.replace(tmp_name, target)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                raise
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        _fail(exc)
    print(str(target))


def register(subparsers, *, resolve_token) -> None:
    parser = subparsers.add_parser(
        "dropbox", help="List or materialize files from the global operator dropbox",
    )
    commands = parser.add_subparsers(dest="dropbox_command", required=True)

    list_parser = commands.add_parser("list", help="List newest dropbox items")
    list_parser.add_argument("--limit", type=int, default=3)
    list_parser.set_defaults(func=cmd_list, _resolve_token=resolve_token)

    get_parser = commands.add_parser("get", help="Materialize one dropbox item locally")
    get_parser.add_argument("id", help="Full id or unique prefix (at least 8 characters)")
    get_parser.add_argument(
        "--output-dir", default="/workspace/output/dropbox",
        help="Destination directory (default /workspace/output/dropbox)",
    )
    get_parser.set_defaults(func=cmd_get, _resolve_token=resolve_token)
