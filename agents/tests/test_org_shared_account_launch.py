"""auto-26e8a: a launch on an organization-shared account opens that
account from the organization's vault, and on a carried (runner) launch it
is the only secret the runner itself provides (graph://7eb29bc8-31a v6 D10)."""

from __future__ import annotations

import json

from agents import session_launcher as sl
from agents.tests.test_session_launcher import (  # noqa: F401 -- fixtures
    _run, captured_run, fake_crosstalk, neutralize_launch_preflight, platform_snapshot,
    signin_deliveries,
)
from tools.dashboard.tests.test_org_shared_accounts import _store


def test_a_launch_on_a_shared_account_delivers_the_organizations_sign_in(
        tmp_path, monkeypatch, fake_crosstalk, captured_run):
    _store(monkeypatch)
    monkeypatch.setattr(sl, "_claude_usage_rows", lambda: [])
    delivered = {}
    monkeypatch.setattr(sl, "deliver_signins",
                        lambda name, payloads, **_k: delivered.update(payloads) or [])
    run = tmp_path / "run"
    _run(name="auto-org", output_dir=str(run), harness="claude",
         account_id="O1", account_org="acme")
    bundle = json.loads(delivered[sl.CLAUDE_BUNDLE_FILENAME])
    assert bundle["claudeAiOauth"]["accessToken"] == "at-ORG"
    meta = json.loads((run / "sessions" / ".session_meta.json").read_text())
    assert (meta["harness_token"], meta["account_selection"]["source"]) == ("O1", "acme")


def test_a_carried_launch_opens_only_the_organizations_account_here(
        tmp_path, monkeypatch, fake_crosstalk, captured_run):
    _store(monkeypatch)
    monkeypatch.setattr(sl, "_claude_usage_rows", lambda: [])
    monkeypatch.setattr(sl, "_resolve_credential",
                        lambda *_a: (_ for _ in ()).throw(AssertionError("runner vault")))
    delivered = {}
    monkeypatch.setattr(sl, "deliver_signins",
                        lambda name, payloads, **_k: delivered.update(payloads) or [])
    _run(name="auto-orgc", output_dir=str(tmp_path / "run"), harness="claude",
         extra_env={"GH_TOKEN": "credential:github.token"},
         account_id="O1", account_org="acme",
         carried=sl.CarriedCredentials({"github.token": "ghp_MEMBER"}))
    assert json.loads(delivered[sl.CLAUDE_BUNDLE_FILENAME])["claudeAiOauth"]["accessToken"] == "at-ORG"
    assert delivered["env.GH_TOKEN"] == b"ghp_MEMBER"


