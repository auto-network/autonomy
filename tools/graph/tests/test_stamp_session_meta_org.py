"""The additive org-stamping migration for legacy-only session metas (auto-5eu2s)."""

from __future__ import annotations

import json
import os

from tools.graph.migrations import stamp_session_meta_org as mig


def _meta(root, run, body, nested=True):
    d = root / run / "sessions" if nested else root / run
    d.mkdir(parents=True)
    p = d / ".session_meta.json"
    p.write_text(body if isinstance(body, str) else json.dumps(body))
    return p


def test_dry_run_changes_nothing_and_apply_stamps_once(tmp_path, capsys):
    has = _meta(tmp_path, "a", {"org": "autonomy", "graph_org": "autonomy"})
    both = _meta(tmp_path, "b", {"graph_org": "anchore", "graph_project": "autonomy"})
    os.chmod(both, 0o640)
    proj = _meta(tmp_path, "c", {"graph_project": "personal"}, nested=False)
    neither = _meta(tmp_path, "d", {"type": "host"})
    _meta(tmp_path, "e", "not json")
    before = {p: p.read_text() for p in (has, both, proj, neither)}

    assert mig.main([str(tmp_path)]) == 0
    assert {p: p.read_text() for p in before} == before
    assert "would stamp: 2" in capsys.readouterr().out

    assert mig.main([str(tmp_path), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "stamped: 2" in out and "'legacy_only': 0" in out
    stamped = json.loads(both.read_text())
    assert stamped == {"graph_org": "anchore", "graph_project": "autonomy", "org": "anchore"}
    assert oct(os.stat(both).st_mode & 0o777) == oct(0o640)
    assert json.loads(proj.read_text())["org"] == "personal"
    assert has.read_text() == before[has] and neither.read_text() == before[neither]

    assert mig.main([str(tmp_path), "--apply"]) == 0
    assert "stamped: 0" in capsys.readouterr().out
