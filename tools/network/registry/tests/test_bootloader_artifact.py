"""Strict validation for the bootloader's executable artifact slices."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


AUTONET_JS = Path(__file__).resolve().parents[1] / "bootloader" / "autonet.js"
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _validate(cases: list[dict]) -> list[bool]:
    script = (
        "const fs=require('fs');"
        f"let src=fs.readFileSync({json.dumps(str(AUTONET_JS))},'utf8');"
        "src=src.replace(/window\\.autonet = autonet;[\\s\\S]*$/,'return autonet;');"
        "src=src.replace(/^const autonet = \\(\\(\\) => \\{/,'');"
        "const A=new Function('TextEncoder','crypto',src)(TextEncoder,{subtle:{}});"
        "const cases=JSON.parse(process.argv[1]);"
        "const out=cases.map(c=>{try{A.validateArtifact(c.header,new Uint8Array(c.size));return true;}catch(e){return false;}});"
        "process.stdout.write(JSON.stringify(out));"
    )
    result = subprocess.run(
        ["node", "-e", script, json.dumps(cases)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _note_header() -> dict:
    return {
        "v": 1, "status": "ok", "kind": "note",
        "viewer": {"offset": 0, "length": 10},
        "content": {
            "title": "T",
            "markdown": {"offset": 10, "length": 5},
            "parts": [
                {"ref": "image", "mime": "image/png", "offset": 15, "length": 5},
            ],
        },
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_valid_note_and_design_headers_are_accepted():
    design = {
        "v": 1, "status": "ok", "kind": "design",
        "viewer": {"offset": 0, "length": 20},
    }
    unicode_title = _note_header()
    unicode_title["content"]["title"] = "😀" * 500
    branded = _note_header()
    branded["branding"] = {
        "name": "Example Org", "color": "#123456", "initial": "E",
        "favicon": {"mime": "image/png", "offset": 20, "length": 3},
    }
    branded_url = _note_header()
    branded_url["branding"] = {
        "name": "Example Org", "color": "#123456", "initial": "E",
        "favicon_url": "https://example.test/favicon.png",
    }
    with_manifest = _note_header()
    with_manifest["content"]["attachments"] = [
        {"ref": "a1", "name": "doc.txt", "mime": "text/plain",
         "raw_sha256": "a" * 64, "total_size": 100, "oversize": False},
        {"ref": "a2", "name": "", "mime": "application/octet-stream",
         "raw_sha256": _EMPTY_SHA256, "total_size": 0, "oversize": True},
    ]
    empty_manifest = _note_header()
    empty_manifest["content"]["attachments"] = []
    assert _validate([
        {"header": _note_header(), "size": 20},
        {"header": unicode_title, "size": 20},
        {"header": design, "size": 20},
        {"header": branded, "size": 23},
        {"header": branded_url, "size": 20},
        {"header": with_manifest, "size": 20},
        {"header": empty_manifest, "size": 20},
    ]) == [True, True, True, True, True, True, True]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_malformed_ranges_refs_and_unions_are_rejected():
    cases = []

    def add(mutator):
        header = _note_header()
        mutator(header)
        cases.append({"header": header, "size": 20})

    add(lambda h: h["viewer"].update(offset=-1))
    add(lambda h: h["viewer"].update(offset=0.5))
    add(lambda h: h["viewer"].update(offset=9007199254740992))
    add(lambda h: h["viewer"].update(length=0))
    add(lambda h: h["content"]["markdown"].update(offset=19, length=2))
    add(lambda h: h["content"]["markdown"].update(offset=9, length=3))
    add(lambda h: h["content"]["markdown"].update(offset=11, length=4))
    add(lambda h: h["content"]["parts"][0].update(length=4))
    add(lambda h: h["content"]["parts"].append(dict(h["content"]["parts"][0])))
    add(lambda h: h.update(kind="design"))
    add(lambda h: h["content"].update(title="x" * 501))
    add(lambda h: h["content"].update(title="😀" * 501))
    add(lambda h: h.update(extra=True))

    add(lambda h: h.update(branding={
        "name": "Org", "color": "red", "initial": "O",
    }))
    add(lambda h: h.update(branding={
        "name": "Org", "color": "#123456", "initial": "OO",
    }))
    add(lambda h: h.update(branding={
        "name": "Org", "color": "#123456", "initial": "O",
        "favicon_url": "http://example.test/icon.png",
    }))
    add(lambda h: h.update(branding={
        "name": "Org", "color": "#123456", "initial": "O",
        "favicon": {"mime": "image/png", "offset": 19, "length": 1},
    }))

    missing_content = _note_header()
    del missing_content["content"]
    cases.append({"header": missing_content, "size": 20})

    def add_manifest(mutator):
        # Base entry is valid so each mutation isolates exactly one defect.
        header = _note_header()
        header["content"]["attachments"] = [{
            "ref": "a1", "name": "n", "mime": "text/plain",
            "raw_sha256": "a" * 64, "total_size": 1, "oversize": False,
        }]
        mutator(header["content"])
        cases.append({"header": header, "size": 20})

    add_manifest(lambda c: c["attachments"][0].update(total_size=-1))
    add_manifest(lambda c: c["attachments"][0].update(total_size=0.5))
    add_manifest(lambda c: c["attachments"][0].update(total_size=True))
    add_manifest(lambda c: c["attachments"][0].update(oversize="yes"))
    add_manifest(lambda c: c["attachments"][0].update(ref=""))
    add_manifest(lambda c: c["attachments"][0].update(mime=""))
    add_manifest(lambda c: c["attachments"][0].pop("raw_sha256"))
    add_manifest(lambda c: c["attachments"][0].update(raw_sha256="deadbeef"))
    add_manifest(lambda c: c["attachments"][0].update(raw_sha256=""))
    add_manifest(lambda c: c["attachments"][0].update(raw_sha256="A" * 64))
    add_manifest(lambda c: c["attachments"][0].update(raw_sha256="a" * 63))
    add_manifest(lambda c: c["attachments"][0].update(raw_sha256="g" * 64))
    add_manifest(lambda c: c["attachments"][0].update(name="x" * 256))
    add_manifest(lambda c: c["attachments"][0].update(extra=True))
    add_manifest(lambda c: c["attachments"].append(dict(c["attachments"][0])))
    add_manifest(lambda c: c.update(attachments="not-a-list"))

    assert _validate(cases) == [False] * len(cases)


# ── generic host: any kind, any parts (auto-ue9md) ──────────────────────
#
# The host used to carry an allowlist of the four viewer kinds it knew, so
# adding a viewer meant a registry deploy -- and a registry deploy restarts
# every live link platform-wide. `kind` is now bounded descriptive metadata
# and `parts` are validated structurally, because what a part MEANS is the
# viewer's business, not the host's.


def _generic_header(kind: str = "quux") -> dict:
    return {
        "v": 1, "status": "ok", "kind": kind,
        "viewer": {"offset": 0, "length": 10},
        "parts": [
            {"ref": "state", "mime": "application/json", "offset": 10, "length": 5},
            {"ref": "body", "mime": "text/html", "offset": 15, "length": 5},
        ],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_an_unknown_kind_with_generic_parts_is_accepted():
    """A viewer type the host has never heard of renders without a deploy."""
    assert _validate([
        {"header": _generic_header(), "size": 20},
        {"header": _generic_header("mission"), "size": 20},
        {"header": _generic_header("a" * 64), "size": 20},
    ]) == [True, True, True]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_kind_is_still_bounded_even_without_an_allowlist():
    cases = []
    for bad in ("", "a" * 65, 1, None, ["note"]):
        header = _generic_header()
        header["kind"] = bad
        cases.append({"header": header, "size": 20})
    assert _validate(cases) == [False] * len(cases)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_generic_parts_are_validated_structurally():
    duplicate = _generic_header()
    duplicate["parts"][1]["ref"] = "state"

    out_of_bounds = _generic_header()
    out_of_bounds["parts"][1]["length"] = 500

    gap = _generic_header()          # slices must exactly cover the body
    gap["parts"][1]["offset"] = 16

    unknown_key = _generic_header()
    unknown_key["parts"][0]["role"] = "primary"

    not_a_list = _generic_header()
    not_a_list["parts"] = {"ref": "state"}

    empty_ref = _generic_header()
    empty_ref["parts"][0]["ref"] = ""

    assert _validate([
        {"header": duplicate, "size": 20},
        {"header": out_of_bounds, "size": 20},
        {"header": gap, "size": 20},
        {"header": unknown_key, "size": 20},
        {"header": not_a_list, "size": 20},
        {"header": empty_ref, "size": 20},
    ]) == [False, False, False, False, False, False]
