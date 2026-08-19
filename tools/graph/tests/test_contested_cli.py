"""``graph set contested`` — stats by default, detail on an exact set.

CLI-layer behavior over a fake client: glob and key filtering, the
stats/detail mode switch, limits, and JSON output. The resolution semantics
behind the numbers live in test_settings_slot_resolution.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tools.graph import set_cmd

PERSONA_A = "aa" * 32
PERSONA_B = "bb" * 32

CONTESTED = {
    "autonomy.org.member-directory": [
        {
            "key": "shared-value",
            "slots": [
                {"terminal_persona": PERSONA_A, "signed_at": 2_000_000,
                 "state": "published", "org": "signedorg", "resolves": True},
                {"terminal_persona": PERSONA_B, "signed_at": 1_000_000,
                 "state": "published", "org": "signedorg", "resolves": False},
            ],
        },
        {
            "key": "workspace:alpha",
            "slots": [
                {"terminal_persona": PERSONA_A, "signed_at": 5_000_000,
                 "state": "canonical", "org": "signedorg", "resolves": True},
                {"terminal_persona": PERSONA_B, "signed_at": 4_000_000,
                 "state": "canonical", "org": "signedorg", "resolves": False},
            ],
        },
    ],
    "autonomy.org.commit-policy": [
        {
            "key": "master",
            "slots": [
                {"terminal_persona": PERSONA_A, "signed_at": 3_000_000,
                 "state": "published", "org": "signedorg", "resolves": False},
                {"terminal_persona": PERSONA_B, "signed_at": 3_500_000,
                 "state": "published", "org": "signedorg", "resolves": True},
            ],
        },
    ],
    "autonomy.workspace.quiet": [],
}


class FakeClient:
    def list_set_ids(self, *, org):
        return list(CONTESTED)

    def contested_keys(self, set_id, *, org):
        return CONTESTED.get(set_id, [])


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    monkeypatch.setattr(set_cmd, "get_client", lambda: FakeClient())


def run(capsys, **overrides):
    args = SimpleNamespace(
        set_id=None, key=None, limit=None, json=False, org=None,
    )
    for name, value in overrides.items():
        setattr(args, name, value)
    set_cmd.cmd_set_contested(args)
    return capsys.readouterr().out


def test_general_form_is_high_level_stats_only(capsys):
    out = run(capsys)
    assert "autonomy.org.member-directory" in out
    assert "autonomy.org.commit-policy" in out
    assert "autonomy.workspace.quiet" not in out, "quiet sets are omitted"
    assert PERSONA_A[:8] not in out, "stats mode never prints slots"
    directory_row = next(
        line for line in out.splitlines()
        if line.startswith("autonomy.org.member-directory")
    )
    assert directory_row.split()[1:3] == ["2", "4"], "KEYS and SLOTS counts"


def test_a_set_glob_filters_the_stats(capsys):
    out = run(capsys, set_id="*.commit-*")
    assert "autonomy.org.commit-policy" in out
    assert "autonomy.org.member-directory" not in out


def test_a_key_glob_filters_within_sets(capsys):
    out = run(capsys, set_id="autonomy.org.*", key="workspace:*")
    assert "autonomy.org.member-directory" in out
    assert "autonomy.org.commit-policy" not in out, (
        "its only key does not match the key glob"
    )


def test_an_exact_set_id_prints_per_slot_detail(capsys):
    out = run(capsys, set_id="autonomy.org.member-directory")
    assert "shared-value" in out and "workspace:alpha" in out
    assert PERSONA_A[:16] in out and PERSONA_B[:16] in out
    resolving = [l for l in out.splitlines() if "→" in l]
    assert len(resolving) == 2, "exactly one resolving marker per key"


def test_limit_caps_and_says_what_it_dropped(capsys):
    out = run(capsys, set_id="autonomy.org.member-directory", limit=1)
    assert "shared-value" in out
    assert "workspace:alpha" not in out
    assert "1 more contested key" in out


def test_json_mode_emits_the_raw_structure(capsys):
    out = run(capsys, set_id="autonomy.org.*", json=True)
    data = json.loads(out)
    assert set(data) == {
        "autonomy.org.member-directory", "autonomy.org.commit-policy",
    }
    assert data["autonomy.org.commit-policy"][0]["slots"][1]["resolves"] is True


def test_nothing_contested_says_so(capsys):
    out = run(capsys, set_id="autonomy.workspace.quiet")
    assert "no contested keys" in out
