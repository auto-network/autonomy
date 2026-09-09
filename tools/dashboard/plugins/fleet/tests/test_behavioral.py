"""L2.B browser acceptance for the read-only Fleet Machines surface."""
from __future__ import annotations

import json

import pytest

from tools.dashboard.test_lib.l2b_harness import _ab_eval_batch, _navigate_and_check


VIEW = {
    "serverTime": 1_777_000_000_000,
    "summary": {
        "authorizedMachines": 1,
        "connectedMachines": None,
        "lastSuccessfulSyncAt": None,
        "joinRequests": 3,
    },
    "machines": [
        {
            "rowKind": "roster_machine", "sourceApprovalId": None,
            "entryId": "entry-local", "machineId": "11" * 32,
            "machinePublicKey": "aa" * 32, "displayLabel": "This dashboard",
            "isLocalMachine": True, "isTunnelServer": True,
            "standing": "authorized", "assignment": "personal_root_holder",
            "standingChangedAt": 1_777_000_000_000, "presence": "unreported",
            "lastSuccessfulSyncAt": None, "transactionsApplied": 0,
            "bytesSent": 0, "bytesReceived": 0, "retryCount": 0,
            "syncIterations": 2, "successfulIterations": 2,
            "failedIterations": 0, "totalSyncDurationMs": 12_500,
            "lastSyncDurationMs": 2_500, "mutationFrames": 4,
            "transactionsTransferred": 2,
            "lastSyncOutcome": "success",
            "lastErrorCode": None, "canRemove": False,
        },
        {
            "rowKind": "pending_admission", "sourceApprovalId": "fleet-one",
            "entryId": None, "machineId": None, "machinePublicKey": None,
            "displayLabel": "New machine", "isLocalMachine": False,
            "isTunnelServer": False, "standing": "pending_approval",
            "assignment": None, "standingChangedAt": 1_776_999_995_000,
            "presence": "not_applicable", "lastSuccessfulSyncAt": None,
            "transactionsApplied": 0, "bytesSent": 0, "bytesReceived": 0,
            "syncIterations": 0, "successfulIterations": 0,
            "failedIterations": 0, "totalSyncDurationMs": 0,
            "lastSyncDurationMs": 0, "mutationFrames": 0,
            "transactionsTransferred": 0,
            "lastSyncOutcome": None,
            "retryCount": 0, "lastErrorCode": None, "canRemove": False,
        },
        {
            "rowKind": "pending_admission", "sourceApprovalId": "fleet-two",
            "entryId": None, "machineId": None, "machinePublicKey": None,
            "displayLabel": "New machine", "isLocalMachine": False,
            "isTunnelServer": False, "standing": "admission_in_progress",
            "assignment": None, "standingChangedAt": 1_776_999_996_000,
            "presence": "not_applicable", "lastSuccessfulSyncAt": None,
            "transactionsApplied": 0, "bytesSent": 0, "bytesReceived": 0,
            "syncIterations": 0, "successfulIterations": 0,
            "failedIterations": 0, "totalSyncDurationMs": 0,
            "lastSyncDurationMs": 0, "mutationFrames": 0,
            "transactionsTransferred": 0,
            "lastSyncOutcome": None,
            "retryCount": 0, "lastErrorCode": None, "canRemove": False,
        },
        {
            "rowKind": "pending_admission", "sourceApprovalId": "fleet-three",
            "entryId": None, "machineId": None, "machinePublicKey": None,
            "displayLabel": "New machine", "isLocalMachine": False,
            "isTunnelServer": False, "standing": "admission_failed",
            "assignment": None, "standingChangedAt": 1_776_999_997_000,
            "presence": "not_applicable", "lastSuccessfulSyncAt": None,
            "transactionsApplied": 0, "bytesSent": 0, "bytesReceived": 0,
            "syncIterations": 0, "successfulIterations": 0,
            "failedIterations": 0, "totalSyncDurationMs": 0,
            "lastSyncDurationMs": 0, "mutationFrames": 0,
            "transactionsTransferred": 0,
            "lastSyncOutcome": None,
            "retryCount": 0, "lastErrorCode": "approval_execution_failed",
            "canRemove": False,
        },
    ],
    "invitation": {
        "status": "active",
        "url": "AUTONOMY_FLEET_INVITE=signed-fixture.checksum",
        "publishedAt": 1_776_999_000_000,
        "expiresAt": 1_777_086_400_000,
        "publishingOrg": "autonomy",
        "error": None,
    },
    "activity": {
        "transactionsApplied": 0, "bytesSent": 0, "bytesReceived": 0,
        "syncIterations": 2, "successfulIterations": 2,
        "failedIterations": 0, "totalSyncDurationMs": 12_500,
        "mutationFrames": 4,
        "scope": "this_dashboard_current_roster",
    },
}


def _install_stub():
    _ab_eval_batch(
        """
        window.__fleetOriginalFetch = window.__fleetOriginalFetch || window.fetch;
        window.__fleetFetches = [];
        window.__fleetView = %s;
        window.fetch = function(input, init) {
          var url = typeof input === 'string' ? input : (input && input.url) || '';
          if (url === '/api/plugins/fleet/view') {
            window.__fleetFetches.push({url: url, method: (init && init.method) || 'GET'});
            return Promise.resolve(new Response(JSON.stringify(window.__fleetView), {
              status: 200, headers: {'Content-Type': 'application/json'}
            }));
          }
          if (url === '/api/identity/status') {
            return Promise.resolve(new Response(JSON.stringify({
              personal_identity: {display_name: 'Jeremy'},
              passkeys: [{credential_id: 'fixture'}], onboarding_needed: false,
              signed_in: true, method: 'passkey', enforced: true,
              gate_disabled: false
            }), {status: 200, headers: {'Content-Type': 'application/json'}}));
          }
          return window.__fleetOriginalFetch.apply(this, arguments);
        };
        """ % json.dumps(VIEW)
    )


@pytest.mark.usefixtures("browser")
class TestFleetPluginL2B:
    def test_contextual_plugin_route_renders_without_a_sidebar_entry(self):
        _navigate_and_check("/sessions", "", wait_ms=300)
        _install_stub()
        result = _navigate_and_check(
            "/fleet",
            """
            r.fragment = !!document.querySelector('[data-testid="fleet-fragment-root"]');
            r.sidebar_entry = !!document.querySelector('[data-page="fleet"]');
            r.fetches = (window.__fleetFetches || []).slice();
            """,
            wait_ms=900,
        )
        assert result["fragment"] is True
        assert result["sidebar_entry"] is False
        assert result["fetches"] == [
            {"url": "/api/plugins/fleet/view", "method": "GET"}
        ]

        identity = _ab_eval_batch(
            """
            return window.AutonomyIdentityIndicator.refresh().then(function() {
              document.querySelector('[data-testid="identity-trigger"]').click();
              var action = document.querySelector(
                '[data-testid="identity-action-plugin-fleet"]');
              return {
                present: !!action,
                visible: !!action && action.offsetParent !== null,
                text: action && action.innerText,
              };
            });
            """
        )
        assert identity["present"] is True
        assert identity["visible"] is True
        assert "Machines" in identity["text"]
        assert "Personal fleet" in identity["text"]

    def test_real_lifecycle_rows_are_visible_without_approval_controls(self):
        _navigate_and_check("/sessions", "", wait_ms=200)
        _install_stub()
        result = _navigate_and_check(
            "/fleet",
            """
            r.rows = Array.from(document.querySelectorAll('.fleet-machine')).map(function(row) {
              return {kind: row.dataset.rowKind, standing: row.dataset.standing,
                      visible: row.offsetParent !== null};
            });
            r.text = document.querySelector('[data-testid="fleet-fragment-root"]').innerText;
            r.buttons = Array.from(document.querySelectorAll(
              '[data-testid="fleet-fragment-root"] button')).map(function(b) {
                return b.innerText.trim();
              });
            var fleetRoot = document.querySelector('[data-testid="fleet-fragment-root"]');
            var fleetData = window.Alpine ? Alpine.$data(fleetRoot) : null;
            var tunnelBadge = document.querySelector('.fleet-machine-title .tunnel');
            r.tunnel = {
              value: fleetData && fleetData.machines[0] && fleetData.machines[0].isTunnelServer,
              text: tunnelBadge && tunnelBadge.textContent,
              display: tunnelBadge && getComputedStyle(tunnelBadge).display,
              html: tunnelBadge && tunnelBadge.outerHTML,
            };
            """,
            wait_ms=500,
        )
        assert result["rows"] == [
            {"kind": "pending_admission", "standing": "pending_approval", "visible": True},
            {"kind": "pending_admission", "standing": "admission_in_progress", "visible": True},
            {"kind": "pending_admission", "standing": "admission_failed", "visible": True},
            {"kind": "roster_machine", "standing": "authorized", "visible": True},
        ]
        assert "Awaiting approval" in result["text"]
        assert "Adding machine" in result["text"]
        assert "Admission failed" in result["text"]
        assert result["tunnel"]["value"] is True, result
        assert result["tunnel"]["text"] is None, result
        assert result["tunnel"]["display"] is None, result
        assert not ({"Grant", "Decline", "Retry", "Remove machine"} & set(result["buttons"]))

    def test_invitation_and_unknown_presence_are_presented_truthfully(self):
        _navigate_and_check("/sessions", "", wait_ms=200)
        _install_stub()
        result = _navigate_and_check(
            "/fleet",
            """
            r.text = document.querySelector('[data-testid="fleet-fragment-root"]').innerText;
            r.invite = document.querySelector('.fleet-invite-code').innerText;
            r.verdict = document.querySelector('.fleet-verdict').innerText;
            r.source_ids_visible = r.text.indexOf('fleet-one') !== -1
              || r.text.indexOf('fleet-two') !== -1
              || r.text.indexOf('fleet-three') !== -1;
            """,
            wait_ms=300,
        )
        assert result["invite"] == VIEW["invitation"]["url"]
        assert "1 machine" in result["verdict"]
        assert result["source_ids_visible"] is False
