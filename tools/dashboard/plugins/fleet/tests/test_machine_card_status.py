"""The machine card's status words, driven through the real page.js.

Two defects this covers, both found 2026-09-10:

* The card asked ``serving()`` with no argument — the personal scope — so on
  sjc-2 it rendered "Tunnel: Serving" while three org connectors were dead. It
  must distinguish "unreachable for the fleet" from "unreachable for anchore".
* ``fleetPage()`` defined STRINGS TWICE in one object literal. The later
  definition silently replaced the earlier, complete one, taking the
  ``link_off`` words with it — so a machine blocked on a deactivated invite
  rendered the false-reassuring "Not synced yet" that the code's own comment
  says it must never show.

Loads the real module in node and calls the real functions; nothing here
inspects source text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_PAGE_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "page.js",
)

_JS = r"""
const fs = require('fs');
global.window = {};
eval(fs.readFileSync(process.env.PAGE_JS, 'utf8'));
const page = fleetPage();
const cases = JSON.parse(process.env.CASES);
const out = cases.map((c) => {
  Object.assign(page, c.state || {});
  const machine = c.machine;
  return {
    word: page.tunnelWord(machine),
    tone: page.tunnelTone(machine),
    title: page.tunnelTitle(machine),
    status: page.statusWord(machine),
    statusTone: page.statusTone(machine),
  };
});
console.log(JSON.stringify(out));
"""


def _render(cases: list) -> list:
    result = subprocess.run(
        ["node", "-e", _JS],
        env={**os.environ, "PAGE_JS": _PAGE_JS, "CASES": json.dumps(cases)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _local(**over):
    machine = {
        "entryId": "entry-local", "isLocalMachine": True,
        "isTunnelServer": True, "rowKind": "roster_machine",
        "standing": "authorized",
        "connectorArmed": True, "tunnelServing": True, "tunnelScopesDown": [],
        "runningBuild": "a", "installedBuild": "a", "certValidUntil": None,
        "lastSuccessfulSyncAt": 1,
    }
    machine.update(over)
    return machine


def _serving_state(machine):
    """servingMachineId is derived from the roster, so seed the roster."""
    return {"view": {"machines": [machine], "invitation": {"status": "active"}}}


def test_every_scope_serving_reads_serving():
    [row] = _render([{
        "machine": _local(), "state": _serving_state(_local()),
    }])
    assert row["word"] == "Serving" and row["tone"] == "good"
    assert row["status"] == "Serving"


def test_only_org_scopes_down_reads_degraded_and_names_them():
    [row] = _render([{
        "machine": _local(
            tunnelServing=False, tunnelScopesDown=["anchore", "dynbench"],
        ),
        "state": _serving_state(_local(
            tunnelServing=False, tunnelScopesDown=["anchore", "dynbench"],
        )),
    }])
    assert row["word"] == "Degraded" and row["tone"] == "warn"
    assert "anchore" in row["title"] and "dynbench" in row["title"]
    # Warn, not failed: the fleet can still reach this dashboard.
    assert row["status"] == "Some tunnels down"
    assert row["statusTone"] == "warn"


def test_personal_down_is_still_down_not_degraded():
    [row] = _render([{
        "machine": _local(tunnelServing=False, tunnelScopesDown=["personal"]),
        "state": _serving_state(
            _local(tunnelServing=False, tunnelScopesDown=["personal"]),
        ),
    }])
    assert row["word"] == "Down" and row["tone"] == "bad"
    assert row["status"] == "Tunnel down" and row["statusTone"] == "failed"


def test_an_unreadable_probe_stays_unknown():
    """A null probe must render nothing, never a guessed claim."""
    [row] = _render([{
        "machine": _local(tunnelServing=None, tunnelScopesDown=[]),
        "state": _serving_state(_local(tunnelServing=None)),
    }])
    assert row["word"] == "Unknown" and row["tone"] == "muted"


def _peer(**over):
    machine = {
        "entryId": "entry-peer", "isLocalMachine": False,
        "lastSuccessfulSyncAt": None, "lastOutcome": None,
        "lastErrorCode": None, "runningBuild": "a", "installedBuild": "a",
    }
    machine.update(over)
    return machine


def test_a_machine_blocked_on_a_dead_invite_says_so():
    [row] = _render([{
        "machine": _peer(),
        "state": {"view": {"machines": [],
                           "invitation": {"status": "awaiting_signature"}}},
    }])
    assert row["status"] == "Invite link off"


def test_a_machine_merely_bootstrapping_does_not():
    [row] = _render([{
        "machine": _peer(),
        "state": {"view": {"machines": [],
                           "invitation": {"status": "active"}}},
    }])
    assert row["status"] == "Not synced yet"
