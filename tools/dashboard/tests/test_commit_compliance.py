"""Tests for D4-1 through D4-7 — DN4 governed-rewrite noncompliance
detection (graph note ``175ff7fc-850`` §2.1).

Deliberately independent of the commit-workflow DAO (D4-8 onward,
DN1/DN2's schema) — every fixture here is a real tmp git repo plus the
already-landed ``tools.graph.commit_policy`` resolver.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.dashboard import commit_compliance as cc
from tools.graph import commit_policy


# ── git fixtures ─────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, env_extra: dict | None = None) -> str:
    env = os.environ.copy()
    env.update(env_extra or {})
    result = subprocess.run(
        ["git", *args],
        capture_output=True, text=True, cwd=str(repo), env=env, check=True,
    )
    return result.stdout


def _commit(
    repo: Path, *, content: str, message: str,
    author: tuple[str, str] | None = None,
    committer: tuple[str, str] | None = None,
    sign: bool = False,
) -> str:
    (repo / "file.txt").write_text(content)
    _git(repo, "add", "file.txt")
    env_extra = {}
    if author:
        env_extra["GIT_AUTHOR_NAME"] = author[0]
        env_extra["GIT_AUTHOR_EMAIL"] = author[1]
    if committer:
        env_extra["GIT_COMMITTER_NAME"] = committer[0]
        env_extra["GIT_COMMITTER_EMAIL"] = committer[1]
    args = ["commit", "-m", message]
    if sign:
        args.insert(1, "-S")
    _git(repo, *args, env_extra=env_extra)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "master")
    _git(r, "config", "user.email", "agent@example.com")
    _git(r, "config", "user.name", "Agent")
    _git(r, "config", "commit.gpgsign", "false")
    return r


@pytest.fixture
def ssh_signing_keys(tmp_path: Path) -> dict:
    """Two SSH keypairs (expected + untrusted) and an allowed_signers file
    naming only the expected key, following the exact commands validated
    against this environment's real ``git``/``ssh-keygen`` binaries."""
    keydir = tmp_path / "keys"
    keydir.mkdir()
    expected = keydir / "expected"
    untrusted = keydir / "untrusted"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(expected)],
                    check=True, capture_output=True)
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(untrusted)],
                    check=True, capture_output=True)
    allowed_signers = keydir / "allowed_signers"
    allowed_signers.write_text(
        f"agent@example.com {(expected.with_suffix('.pub')).read_text()}"
    )
    return {
        "expected_key": expected, "expected_pub": expected.with_suffix(".pub"),
        "untrusted_key": untrusted, "untrusted_pub": untrusted.with_suffix(".pub"),
        "allowed_signers": allowed_signers,
    }


def _configure_ssh_signing(repo: Path, keys: dict, signing_key: Path) -> None:
    _git(repo, "config", "gpg.format", "ssh")
    _git(repo, "config", "user.signingkey", str(signing_key))
    _git(repo, "config", "gpg.ssh.allowedSignersFile", str(keys["allowed_signers"]))


# ── policy-member fixtures (bypass the Settings store; feed resolver directly) ──


class _FakeMember:
    def __init__(self, payload: dict, id_: str):
        self.payload = payload
        self.id = id_


def _resolved(repo_slug: str, payload: dict) -> commit_policy.ResolvedCommitPolicy:
    members = {f"repo:{repo_slug}": _FakeMember(payload, f"member:{repo_slug}")}
    return commit_policy.resolve_commit_policy_from_members(members=members, repo_slug=repo_slug)


def _patch_resolver(monkeypatch, mapping: dict[str, dict]):
    """mapping: repo_slug -> policy payload. Anything else falls to safe.default."""
    def fake_resolve(*, repo_slug=None, org=None, **_kw):
        if repo_slug in mapping:
            return _resolved(repo_slug, mapping[repo_slug])
        return commit_policy.resolve_commit_policy_from_members(members={}, repo_slug=repo_slug)
    monkeypatch.setattr(cc, "resolve_commit_policy", fake_resolve)


# Raw payload dicts (no "profile" key) deliberately bypass
# expand_commit_policy_payload's override-mode validation — that
# validation exists for the Settings-authoring path (Codex/DN1-owned),
# not for exercising this module's detection logic against arbitrary
# policy shapes.
SIG_NONE_NO_SIGNOFF = {
    "signature_requirement": "none",
    "author_policy": {"require_operator_confirmation": False, "require_signoff": False},
}

SIG_REQUIRED_STRICT = {
    "signature_requirement": "signoff_and_ssh",
    "author_policy": {"require_operator_confirmation": True, "require_signoff": True},
}


# ── D4-1: ComplianceReport shape / round-trip ──────────────────────────


def _sample_report() -> cc.ComplianceReport:
    return cc.ComplianceReport(
        commit_sha="a" * 40,
        resolved_policy_version="repo:demo/repo",
        sign_off=cc.SignOffStatus(
            required=True, present=True, trailer_value="Ada <ada@example.com>",
            matches_policy_identity=True,
        ),
        authorship=cc.AuthorshipStatus(
            required_identity={"name": "Ada", "email": "ada@example.com"},
            actual_author={"name": "Ada", "email": "ada@example.com"},
            actual_committer={"name": "Ada", "email": "ada@example.com"},
            author_matches=True, committer_matches=True,
        ),
        signature=cc.SignatureStatus(
            required="ssh", present=True, kind="ssh", valid=True,
            verified_key_fingerprint="SHA256:deadbeef",
            verification_method="git verify-commit",
        ),
        compliant=True,
        violations=(),
    )


def test_D4_1_report_round_trips_through_json():
    import json
    report = _sample_report()
    parsed = json.loads(json.dumps(report.to_dict()))
    restored = cc.ComplianceReport.from_dict(parsed)
    assert restored == report


def test_D4_1_report_rejects_unknown_top_level_field():
    payload = _sample_report().to_dict()
    payload["unexpected_field"] = "surprise"
    with pytest.raises(ValueError):
        cc.ComplianceReport.from_dict(payload)


def test_D4_1_valid_violation_codes_cover_the_spec_minimum():
    minimum = {
        "signoff_missing", "signoff_identity_mismatch",
        "author_mismatch", "signature_absent", "signature_invalid",
    }
    assert minimum <= cc.VALID_VIOLATION_CODES


# ── D4-2: target policy, never source ──────────────────────────────────


def test_D4_2_resolves_against_target_repo_slug_not_source(repo, monkeypatch):
    _patch_resolver(monkeypatch, {
        "source/repo": SIG_NONE_NO_SIGNOFF,
        "target/repo": SIG_REQUIRED_STRICT,
    })
    sha = _commit(repo, content="x", message="msg")

    reports = cc.audit_compliance(
        repo_slug="target/repo", commit_shas=[sha], cwd=repo,
        target_branch="master", rewrite_context=True,
    )
    assert reports[0].resolved_policy_version == "repo:target/repo"
    assert reports[0].sign_off.required is True  # strict target policy, not the permissive source


def test_D4_2_rewrite_context_without_target_branch_raises(repo, monkeypatch):
    _patch_resolver(monkeypatch, {"target/repo": SIG_NONE_NO_SIGNOFF})
    sha = _commit(repo, content="x", message="msg")
    with pytest.raises(commit_policy.CommitPolicyError):
        cc.audit_compliance(
            repo_slug="target/repo", commit_shas=[sha], cwd=repo,
            target_branch=None, rewrite_context=True,
        )


# ── D4-3: sign-off axis ─────────────────────────────────────────────────


def test_D4_3_no_trailer_yields_signoff_missing(repo, monkeypatch):
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    monkeypatch.setattr(cc, "OPERATOR_IDENTITY_PROVIDER", lambda: {"name": "Op", "email": "op@example.com"})
    sha = _commit(repo, content="x", message="no trailer here")

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.sign_off.present is False
    assert "signoff_missing" in report.violations
    assert "signoff_identity_mismatch" not in report.violations


def test_D4_3_matching_trailer_yields_no_violation(repo, monkeypatch):
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    monkeypatch.setattr(cc, "OPERATOR_IDENTITY_PROVIDER", lambda: {"name": "Op", "email": "op@example.com"})
    sha = _commit(repo, content="x", message="msg\n\nSigned-off-by: Op <op@example.com>")

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.sign_off.present is True
    assert report.sign_off.matches_policy_identity is True
    assert "signoff_missing" not in report.violations
    assert "signoff_identity_mismatch" not in report.violations


def test_D4_3_wrong_identity_trailer_yields_identity_mismatch_not_missing(repo, monkeypatch):
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    monkeypatch.setattr(cc, "OPERATOR_IDENTITY_PROVIDER", lambda: {"name": "Op", "email": "op@example.com"})
    sha = _commit(repo, content="x", message="msg\n\nSigned-off-by: Eve <eve@example.com>")

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.sign_off.present is True
    assert report.sign_off.matches_policy_identity is False
    assert "signoff_identity_mismatch" in report.violations
    assert "signoff_missing" not in report.violations


def test_D4_3_no_identity_requirement_present_trailer_passes(repo, monkeypatch):
    lenient = {
        "signature_requirement": "none",
        "author_policy": {"require_operator_confirmation": False, "require_signoff": True},
    }
    _patch_resolver(monkeypatch, {"r": lenient})
    sha = _commit(repo, content="x", message="msg\n\nSigned-off-by: Whoever <whoever@example.com>")

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.sign_off.matches_policy_identity is True
    assert not report.violations


# ── D4-4: authorship axis (author/committer independent) ──────────────


def test_D4_4_author_matches_committer_does_not(repo, monkeypatch):
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    monkeypatch.setattr(cc, "OPERATOR_IDENTITY_PROVIDER", lambda: {"name": "Op", "email": "op@example.com"})
    sha = _commit(
        repo, content="x", message="msg\n\nSigned-off-by: Op <op@example.com>",
        author=("Op", "op@example.com"), committer=("Agent Bot", "bot@example.com"),
    )

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.authorship.author_matches is True
    assert report.authorship.committer_matches is False
    assert "committer_mismatch" in report.violations
    assert "author_mismatch" not in report.violations


def test_D4_4_no_constraint_policy_both_pass(repo, monkeypatch):
    _patch_resolver(monkeypatch, {"r": SIG_NONE_NO_SIGNOFF})
    sha = _commit(
        repo, content="x", message="msg",
        author=("Whoever", "whoever@example.com"), committer=("Someone Else", "else@example.com"),
    )

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.authorship.author_matches is True
    assert report.authorship.committer_matches is True
    assert not report.violations


# ── operator-identity seam: unresolved must fail closed ────────────────


def test_unresolved_operator_identity_fails_closed_not_silently_compliant(repo, monkeypatch):
    """Do NOT patch OPERATOR_IDENTITY_PROVIDER — exercise the real default
    stub (returns None). Even a commit whose trailer/author/committer
    literally spell out the operator's identity string must NOT be
    reported as matching, because there is no real identity to compare
    against — a stubbed-out provider must never make a commit look
    compliant. (Fable's finding on the D4-1..D4-7 review.)"""
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    sha = _commit(
        repo, content="x", message="msg\n\nSigned-off-by: Op <op@example.com>",
        author=("Op", "op@example.com"), committer=("Op", "op@example.com"),
    )

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.sign_off.matches_policy_identity is False
    assert report.authorship.author_matches is False
    assert report.authorship.committer_matches is False
    assert report.authorship.required_identity == {"unresolved": True}
    assert report.compliant is False
    assert "signoff_identity_mismatch" in report.violations
    assert "author_mismatch" in report.violations
    assert "committer_mismatch" in report.violations


def test_unresolved_operator_identity_still_reports_signoff_missing_when_absent(repo, monkeypatch):
    """The unresolved-identity fail-closed path must not mask a more basic
    violation — no trailer at all is still signoff_missing, not
    signoff_identity_mismatch."""
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    sha = _commit(repo, content="x", message="no trailer")

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert "signoff_missing" in report.violations
    assert "signoff_identity_mismatch" not in report.violations


# ── D4-5: signature axis via git verify-commit ─────────────────────────


def test_D4_5_unsigned_commit_yields_signature_absent(repo, monkeypatch):
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    monkeypatch.setattr(cc, "OPERATOR_IDENTITY_PROVIDER", lambda: {"name": "Op", "email": "op@example.com"})
    sha = _commit(
        repo, content="x", message="msg\n\nSigned-off-by: Op <op@example.com>",
        author=("Op", "op@example.com"), committer=("Op", "op@example.com"),
    )

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.signature.present is False
    assert report.signature.valid is False
    assert "signature_absent" in report.violations
    assert "signature_invalid" not in report.violations


def test_D4_5_signed_with_untrusted_key_yields_signature_invalid_not_absent(repo, monkeypatch, ssh_signing_keys):
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    monkeypatch.setattr(cc, "OPERATOR_IDENTITY_PROVIDER", lambda: {"name": "Op", "email": "op@example.com"})
    _configure_ssh_signing(repo, ssh_signing_keys, ssh_signing_keys["untrusted_key"])
    sha = _commit(
        repo, content="x", message="msg\n\nSigned-off-by: Op <op@example.com>",
        author=("Op", "op@example.com"), committer=("Op", "op@example.com"), sign=True,
    )

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.signature.present is True, "a gpgsig block exists — presence alone must not be mistaken for absence"
    assert report.signature.valid is False
    assert "signature_invalid" in report.violations
    assert "signature_absent" not in report.violations


def test_D4_5_signed_with_expected_key_verifies(repo, monkeypatch, ssh_signing_keys):
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    monkeypatch.setattr(cc, "OPERATOR_IDENTITY_PROVIDER", lambda: {"name": "Op", "email": "op@example.com"})
    _configure_ssh_signing(repo, ssh_signing_keys, ssh_signing_keys["expected_key"])
    sha = _commit(
        repo, content="x", message="msg\n\nSigned-off-by: Op <op@example.com>",
        author=("Op", "op@example.com"), committer=("Op", "op@example.com"), sign=True,
    )

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.signature.valid is True
    assert report.signature.verified_key_fingerprint
    assert not report.violations


# ── D4-6: combined compliant flag honors only applicable requirements ─


def test_D4_6_unsigned_commit_compliant_under_sig_none_policy(repo, monkeypatch):
    _patch_resolver(monkeypatch, {"r": SIG_NONE_NO_SIGNOFF})
    sha = _commit(repo, content="x", message="msg")

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.compliant is True


def test_D4_6_same_commit_noncompliant_under_signature_required_policy(repo, monkeypatch):
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    monkeypatch.setattr(cc, "OPERATOR_IDENTITY_PROVIDER", lambda: {"name": "Op", "email": "op@example.com"})
    sha = _commit(
        repo, content="x", message="msg\n\nSigned-off-by: Op <op@example.com>",
        author=("Op", "op@example.com"), committer=("Op", "op@example.com"),
    )

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.compliant is False
    assert "signature_absent" in report.violations


def test_D4_6_all_axes_passing_is_compliant(repo, monkeypatch, ssh_signing_keys):
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    monkeypatch.setattr(cc, "OPERATOR_IDENTITY_PROVIDER", lambda: {"name": "Op", "email": "op@example.com"})
    _configure_ssh_signing(repo, ssh_signing_keys, ssh_signing_keys["expected_key"])
    sha = _commit(
        repo, content="x", message="msg\n\nSigned-off-by: Op <op@example.com>",
        author=("Op", "op@example.com"), committer=("Op", "op@example.com"), sign=True,
    )

    report = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")[0]
    assert report.compliant is True
    assert not report.violations


# ── D4-7: chain of commit SHAs, one report per commit, in order ────────


def test_D4_7_three_sha_chain_returns_three_reports_in_order(repo, monkeypatch):
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    monkeypatch.setattr(cc, "OPERATOR_IDENTITY_PROVIDER", lambda: {"name": "Op", "email": "op@example.com"})
    sha1 = _commit(repo, content="1", message="one")
    sha2 = _commit(repo, content="2", message="two")
    sha3 = _commit(repo, content="3", message="three")

    reports = cc.audit_compliance(
        repo_slug="r", commit_shas=[sha1, sha2, sha3], cwd=repo, target_branch="master",
    )
    assert [r.commit_sha for r in reports] == [sha1, sha2, sha3]
    assert all(r.resolved_policy_version == "repo:r" for r in reports)


def test_D4_7_single_sha_call_shares_report_shape_with_multi(repo, monkeypatch):
    _patch_resolver(monkeypatch, {"r": SIG_REQUIRED_STRICT})
    monkeypatch.setattr(cc, "OPERATOR_IDENTITY_PROVIDER", lambda: {"name": "Op", "email": "op@example.com"})
    sha = _commit(repo, content="x", message="solo")

    reports = cc.audit_compliance(repo_slug="r", commit_shas=[sha], cwd=repo, target_branch="master")
    assert len(reports) == 1
    assert set(reports[0].to_dict()) == set(_sample_report().to_dict())
