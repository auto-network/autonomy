"""Governed rewrite — noncompliance detection (D4-1 through D4-7).

DN4 (graph note ``175ff7fc-850``) §2.1: ``commit.audit_compliance``
independently checks three axes — sign-off, authorship, and signature —
against the policy resolved for where a rewrite will *land*, never the
commit's current location. This module is deliberately independent of the
commit-workflow DAO tables (``commit_workflow_states`` /
``commit_workflow_commits``, DN1/DN2's schema); the rewrite-flow tasks that
build on top of it (D4-8 onward) own that integration separately, so this
module only needs git plus the already-landed commit-policy resolver
(:mod:`tools.graph.commit_policy`).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, fields
from pathlib import Path

from tools.graph.commit_policy import (
    CommitPolicyError,
    _requires_gpg,
    _requires_ssh,
    resolve_commit_policy,
)

VALID_VIOLATION_CODES = frozenset({
    "signoff_missing",
    "signoff_identity_mismatch",
    "author_mismatch",
    "committer_mismatch",
    "signature_absent",
    "signature_invalid",
})


def _git(args: list[str], cwd: Path, *, timeout: int = 15) -> tuple[int, str, str]:
    """Run git and return (rc, stdout, stderr); never raises on non-zero exit."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return 127, "", str(e)
    return r.returncode, r.stdout, r.stderr


class _ModelMixin:
    """Minimal to_dict for the frozen dataclasses below — round-trips
    through plain dicts (JSON-serializable) without loss."""

    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = value.to_dict() if isinstance(value, _ModelMixin) else value
        return out


@dataclass(frozen=True)
class SignOffStatus(_ModelMixin):
    required: bool
    present: bool
    trailer_value: str | None
    matches_policy_identity: bool


@dataclass(frozen=True)
class AuthorshipStatus(_ModelMixin):
    required_identity: dict | None
    actual_author: dict
    actual_committer: dict
    author_matches: bool
    committer_matches: bool


@dataclass(frozen=True)
class SignatureStatus(_ModelMixin):
    required: str  # "none" | "gpg" | "ssh"
    present: bool
    kind: str | None
    valid: bool
    verified_key_fingerprint: str | None
    verification_method: str


@dataclass(frozen=True)
class ComplianceReport(_ModelMixin):
    commit_sha: str
    resolved_policy_version: str
    sign_off: SignOffStatus
    authorship: AuthorshipStatus
    signature: SignatureStatus
    compliant: bool
    violations: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        out = super().to_dict()
        out["violations"] = list(self.violations)
        return out

    @classmethod
    def from_dict(cls, payload: dict) -> "ComplianceReport":
        unknown = set(payload) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"ComplianceReport.from_dict got unknown fields: {sorted(unknown)}")
        return cls(
            commit_sha=payload["commit_sha"],
            resolved_policy_version=payload["resolved_policy_version"],
            sign_off=SignOffStatus(**payload["sign_off"]),
            authorship=AuthorshipStatus(**payload["authorship"]),
            signature=SignatureStatus(**payload["signature"]),
            compliant=payload["compliant"],
            violations=tuple(payload.get("violations", ())),
        )


# ── D4-2: resolve policy against the TARGET, never the source ─────────


def _resolve_target_policy(*, repo_slug: str, target_branch: str | None, org, rewrite_context: bool):
    """Resolve policy for where a rewrite will land.

    ``repo_slug`` is the caller's responsibility to supply as the
    *destination* repo — policy in this schema is repo-scoped, not
    branch-scoped, so "resolve against the target, not the source" means
    the caller must pass the destination's repo_slug, never the commit's
    current one. ``target_branch`` is required in rewrite context so a
    caller can't silently omit stating what it's rewriting toward.
    """
    if rewrite_context and not target_branch:
        raise CommitPolicyError("audit_compliance in rewrite context requires target_branch")
    resolved = resolve_commit_policy(repo_slug=repo_slug, org=org)
    return resolved


# ── operator-identity seam ──────────────────────────────────────────────
#
# ``author_policy`` in the landed commit-policy schema (tools/graph/
# commit_policy.py, Codex/DN1-owned) currently only carries the booleans
# ``require_operator_confirmation`` / ``require_signoff`` — DN4 §2.1's
# authorship axis needs an actual {name, email} to compare against for
# profiles like "must be the operator's known identity". Rather than
# reaching into commit_policy.py's schema (single-owner, out of scope for
# D4-1..D4-7), this is an injectable seam: production wiring supplies the
# real lookup once one exists; tests supply a fixture identity directly.


def _default_operator_identity_provider() -> dict | None:
    return None


OPERATOR_IDENTITY_PROVIDER = _default_operator_identity_provider


def _required_identity(author_policy: dict) -> dict | None:
    explicit = author_policy.get("required_identity")
    if explicit:
        return explicit
    if author_policy.get("require_operator_confirmation"):
        return OPERATOR_IDENTITY_PROVIDER()
    return None


# ── D4-3: sign-off axis ─────────────────────────────────────────────────


def _signoff_trailer(commit_sha: str, cwd: Path) -> str | None:
    rc, out, _err = _git(
        ["log", "-1", "--format=%(trailers:key=Signed-off-by,valueonly,separator=%x1f)", commit_sha],
        cwd,
    )
    if rc != 0:
        return None
    value = out.split("\x1f")[0].strip()
    return value or None


def _check_signoff(commit_sha: str, cwd: Path, author_policy: dict) -> tuple[SignOffStatus, list[str]]:
    required = bool(author_policy.get("require_signoff"))
    trailer_value = _signoff_trailer(commit_sha, cwd)
    present = trailer_value is not None
    required_identity = _required_identity(author_policy)
    matches = True
    violations: list[str] = []

    if not present:
        matches = False
        if required:
            violations.append("signoff_missing")
    elif required_identity:
        expected = f"{required_identity.get('name')} <{required_identity.get('email')}>"
        matches = trailer_value == expected
        if required and not matches:
            violations.append("signoff_identity_mismatch")

    return SignOffStatus(
        required=required, present=present, trailer_value=trailer_value,
        matches_policy_identity=matches,
    ), violations


# ── D4-4: authorship axis (author/committer reported separately) ──────


def _commit_identities(commit_sha: str, cwd: Path) -> tuple[dict, dict]:
    rc, out, _err = _git(
        ["show", "-s", f"--format=%an\x1f%ae\x1f%cn\x1f%ce", commit_sha], cwd,
    )
    an, ae, cn, ce = (out.strip().split("\x1f") + ["", "", "", ""])[:4]
    return {"name": an, "email": ae}, {"name": cn, "email": ce}


def _check_authorship(commit_sha: str, cwd: Path, author_policy: dict) -> tuple[AuthorshipStatus, list[str]]:
    actual_author, actual_committer = _commit_identities(commit_sha, cwd)
    required_identity = _required_identity(author_policy)
    violations: list[str] = []

    if not required_identity:
        author_matches = True
        committer_matches = True
    else:
        author_matches = (
            actual_author["email"] == required_identity.get("email")
            and actual_author["name"] == required_identity.get("name")
        )
        committer_matches = (
            actual_committer["email"] == required_identity.get("email")
            and actual_committer["name"] == required_identity.get("name")
        )
        if not author_matches:
            violations.append("author_mismatch")
        if not committer_matches:
            violations.append("committer_mismatch")

    return AuthorshipStatus(
        required_identity=required_identity,
        actual_author=actual_author, actual_committer=actual_committer,
        author_matches=author_matches, committer_matches=committer_matches,
    ), violations


# ── D4-5: signature axis via git verify-commit ─────────────────────────
#
# Reuses commit_policy.py's own ``_requires_gpg``/``_requires_ssh`` rather
# than a second hand-copied strength table, so the two never silently
# drift apart if the signature_requirement vocabulary changes.


def _check_signature(commit_sha: str, cwd: Path, signature_requirement: str) -> tuple[SignatureStatus, list[str]]:
    if _requires_gpg(signature_requirement):
        required = "gpg"
    elif _requires_ssh(signature_requirement):
        required = "ssh"
    else:
        required = "none"

    _rc_status, status_code, _e1 = _git(["log", "-1", "--format=%G?", commit_sha], cwd)
    status_code = status_code.strip()
    present = status_code != "N" and status_code != ""

    violations: list[str] = []
    if not present:
        return SignatureStatus(
            required=required, present=False, kind=None, valid=False,
            verified_key_fingerprint=None, verification_method="git verify-commit",
        ), (["signature_absent"] if required != "none" else [])

    verify_rc, _out, _err = _git(["verify-commit", commit_sha], cwd)
    valid = verify_rc == 0
    _rc_fp, fingerprint, _e2 = _git(["log", "-1", "--format=%GF", commit_sha], cwd)
    fingerprint = fingerprint.strip() or None
    kind = "ssh" if fingerprint and fingerprint.startswith("SHA256:") else ("gpg" if fingerprint else None)

    if not valid and required != "none":
        violations.append("signature_invalid")

    return SignatureStatus(
        required=required, present=True, kind=kind, valid=valid,
        verified_key_fingerprint=fingerprint, verification_method="git verify-commit",
    ), violations


# ── D4-6: combined compliant flag ──────────────────────────────────────


def _compliant(violations: list[str]) -> bool:
    return len(violations) == 0


# ── D4-1/D4-2/D4-7: the public entry point ─────────────────────────────


def audit_compliance(
    *,
    repo_slug: str,
    commit_shas: list[str],
    cwd: Path,
    target_branch: str | None = None,
    org=None,
    rewrite_context: bool = True,
) -> list[ComplianceReport]:
    """Audit an ordered list of commit SHAs against the TARGET's resolved
    policy. Single-SHA and multi-SHA calls share this one code path."""
    resolved = _resolve_target_policy(
        repo_slug=repo_slug, target_branch=target_branch, org=org, rewrite_context=rewrite_context,
    )
    author_policy = resolved.payload.get("author_policy") or {}
    signature_requirement = resolved.payload.get("signature_requirement", "none")

    reports = []
    for sha in commit_shas:
        signoff_status, signoff_violations = _check_signoff(sha, cwd, author_policy)
        authorship_status, authorship_violations = _check_authorship(sha, cwd, author_policy)
        signature_status, signature_violations = _check_signature(sha, cwd, signature_requirement)

        violations = signoff_violations + authorship_violations + signature_violations
        reports.append(ComplianceReport(
            commit_sha=sha,
            resolved_policy_version=resolved.key,
            sign_off=signoff_status,
            authorship=authorship_status,
            signature=signature_status,
            compliant=_compliant(violations),
            violations=tuple(violations),
        ))
    return reports
