"""Strict validation for the bootloader's executable artifact slices."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


AUTONET_JS = Path(__file__).resolve().parents[1] / "bootloader" / "autonet.js"


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
    assert _validate([
        {"header": _note_header(), "size": 20},
        {"header": unicode_title, "size": 20},
        {"header": design, "size": 20},
    ]) == [True, True, True]


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

    missing_content = _note_header()
    del missing_content["content"]
    cases.append({"header": missing_content, "size": 20})

    assert _validate(cases) == [False] * len(cases)
