#!/usr/bin/env python3
"""Self-validating Tamarin harness for the storagekit re-key models.

Contract (mirrors tools/dashboard/TLA/run_tlc.py's green/calibration
discipline): the green theory must fully verify AND the calibration
theory — the same protocol with the frontier marker deleted — must
falsify exclusion_forward by finding the F-001 attack trace. A harness
where the calibration passes is broken and exits nonzero exactly like a
failed proof.

Usage:
    python3 run_tamarin.py            # run both, enforce expectations
    python3 run_tamarin.py --trace    # also dump the calibration attack

Requires tamarin-prover and maude on PATH (override with TAMARIN_BIN /
add maude's dir to PATH). Install: see MODEL.md.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# theory file -> {lemma: expected result}
EXPECTATIONS = {
    "VaultRekeyMarker.spthy": {
        "executable_end_to_end": "verified",
        "exclusion_forward": "verified",
        "root_secret": "verified",
        "old_generation_stays_readable": "verified",
    },
    "VaultRekeyF001.spthy": {
        "executable_end_to_end": "verified",
        "exclusion_forward": "falsified",
        "root_secret": "verified",
        "old_generation_stays_readable": "verified",
    },
    "VaultFleetDist.spthy": {
        "executable_end_to_end": "verified",
        "machine_key_yields_nothing_undistributed": "verified",
        "kicked_machine_excluded": "verified",
        "persona_kem_snapshot_total": "verified",
        "root_secret": "verified",
        "machine_key_reach_via_distribution": "verified",
    },
    "VaultFleetDistNoGuard.spthy": {
        "executable_end_to_end": "verified",
        "machine_key_yields_nothing_undistributed": "verified",
        "kicked_machine_excluded": "falsified",
        "persona_kem_snapshot_total": "verified",
        "root_secret": "verified",
        "machine_key_reach_via_distribution": "verified",
    },
    "VaultOpenStore.spthy": {
        "executable": "verified",
        "root_secret_open_store": "verified",
        "no_key_to_forged_pk": "verified",
        "unlock_only_yields_no_root": "verified",
        "session_only_for_enrolled_factor": "verified",
    },
    "VaultOpenStoreNoArmorVerify.spthy": {
        "executable": "verified",
        "root_secret_open_store": "verified",
        "no_key_to_forged_pk": "verified",
        "unlock_only_yields_no_root": "verified",
        "session_only_for_enrolled_factor": "falsified",
    },
    "VaultOpenStoreNoTierVerify.spthy": {
        "executable": "verified",
        "root_secret_open_store": "verified",
        "no_key_to_forged_pk": "falsified",
        "unlock_only_yields_no_root": "verified",
        "session_only_for_enrolled_factor": "verified",
    },
    "VaultRecoveryRace.spthy": {
        "executable": "verified",
        "thief_self_rekey_possible": "verified",
        "recovery_key_secret": "verified",
        "recovery_beats_thief": "verified",
    },
    "VaultRecoveryRaceNoRevoke.spthy": {
        "executable": "verified",
        "thief_self_rekey_possible": "verified",
        "recovery_key_secret": "verified",
        "recovery_beats_thief": "falsified",
    },
    "VaultConcurrentRekey.spthy": {
        "executable": "verified",
        "concurrent_grant_secret": "verified",
        "loser_converges": "verified",
        "either_can_win": "verified",
        "b_can_win": "verified",
    },
    "VaultConcurrentRekeyNoConverge.spthy": {
        "executable": "verified",
        "concurrent_grant_secret": "verified",
        "loser_converges": "falsified",
        "either_can_win": "verified",
        "b_can_win": "verified",
    },
    "VaultRecoverySuccession.spthy": {
        "executable": "verified",
        "veto_possible": "verified",
        "window_cannot_be_fast_forwarded": "verified",
        "cancellation_blocks_completion": "verified",
        "veto_blocks_completion": "verified",
        "completion_implies_witnessed_declaration": "verified",
    },
    "VaultRecoverySuccessionNoCancel.spthy": {
        "executable": "verified",
        "veto_possible": "verified",
        "window_cannot_be_fast_forwarded": "verified",
        "cancellation_blocks_completion": "falsified",
        "veto_blocks_completion": "verified",
        "completion_implies_witnessed_declaration": "verified",
    },
    "VaultFactorPolicy.spthy": {
        "executable": "verified",
        "password_and_passkey1_release_root": "verified",
        "password_and_passkey2_release_root": "verified",
        "password_alone_no_root": "verified",
        "passkey1_alone_no_root": "verified",
        "passkey2_alone_no_root": "verified",
        "passkeys_both_no_root": "verified",
        "unlock_only_no_root": "verified",
    },
    "VaultFactorPolicyNoSplit.spthy": {
        "executable": "verified",
        "password_and_passkey1_release_root": "verified",
        "password_and_passkey2_release_root": "verified",
        "password_alone_no_root": "falsified",
        "passkey1_alone_no_root": "verified",
        "passkey2_alone_no_root": "verified",
        "passkeys_both_no_root": "verified",
        "unlock_only_no_root": "verified",
    },
    "VaultPolicyClass.spthy": {
        "executable": "verified",
        "revoked_factor_excluded_forward": "verified",
        "revoked_factor_keeps_old": "verified",
        "root_reaches_every_generation": "verified",
    },
    "VaultPolicyClassNoReseal.spthy": {
        "executable": "verified",
        "revoked_factor_excluded_forward": "falsified",
        "revoked_factor_keeps_old": "verified",
        "root_reaches_every_generation": "verified",
    },
    "VaultPolicyClassNoAnchor.spthy": {
        "executable": "verified",
        "revoked_factor_excluded_forward": "verified",
        "revoked_factor_keeps_old": "verified",
        "root_reaches_every_generation": "falsified",
    },
    "VaultRootRotation.spthy": {
        "owner_rotates_away_from_stolen_root": "verified",
        "thief_cannot_rotate": "verified",
        "code_finder_cannot_rotate": "verified",
        "both_secrets_suffice": "verified",
        "recovery_pk_is_declared": "verified",
    },
    "VaultRootRotationNoCosign.spthy": {
        "owner_rotates_away_from_stolen_root": "verified",
        "thief_cannot_rotate": "falsified",
        "code_finder_cannot_rotate": "verified",
        "both_secrets_suffice": "verified",
        "recovery_pk_is_declared": "verified",
    },
    "VaultDelegateChain.spthy": {
        "executable": "verified",
        "write_resolves_to_current_member": "verified",
        "delegate_scopes_only": "verified",
        "write_requires_live_delegate": "verified",
        "stolen_delegate_write_witness": "verified",
        "revocation_and_expiry_possible": "verified",
    },
    "VaultDelegateChainNoResolve.spthy": {
        "executable": "verified",
        "write_resolves_to_current_member": "falsified",
        "delegate_scopes_only": "verified",
        "write_requires_live_delegate": "verified",
        "stolen_delegate_write_witness": "verified",
        "revocation_and_expiry_possible": "verified",
    },
    "VaultD006Window.spthy": {
        "executable_halt": "verified",
        "executable_continue": "verified",
        "halt_window_confidential": "verified",
        "continue_window_reaches_removed": "verified",
        "post_rekey_excluded": "verified",
        "removed_machine_keeps_prior": "verified",
    },
    "VaultD006WindowNoHaltGate.spthy": {
        "executable_halt": "verified",
        "executable_continue": "verified",
        "halt_window_confidential": "falsified",
        "continue_window_reaches_removed": "verified",
        "post_rekey_excluded": "verified",
        "removed_machine_keeps_prior": "verified",
    },
    "VaultWitnessAccountability.spthy": {
        "executable": "verified",
        "fraud_story_executable": "verified",
        "poll_yields_alert_or_fraud_proof": "verified",
        "honest_window_cannot_be_fast_forwarded": "verified",
        "honest_cancellation_blocks_completion": "verified",
        "honest_veto_blocks_completion": "verified",
        "honest_completion_implies_witnessed_declaration": "verified",
        "cancel_possible": "verified",
        "veto_possible": "verified",
    },
    # fraud_story_executable is deliberately absent below: its subject IS the
    # deleted position binding (see the theory header and MODEL.md model 12).
    "VaultWitnessAccountabilityNoChainBind.spthy": {
        "executable": "verified",
        "poll_yields_alert_or_fraud_proof": "falsified",
        "honest_window_cannot_be_fast_forwarded": "verified",
        "honest_cancellation_blocks_completion": "verified",
        "honest_veto_blocks_completion": "verified",
        "honest_completion_implies_witnessed_declaration": "verified",
        "cancel_possible": "verified",
        "veto_possible": "verified",
    },
}

SUMMARY_RE = re.compile(
    r"^\s{2}(\w+) \((?:all-traces|exists-trace)\): (verified|falsified)", re.M
)


def run_theory(path: Path, extra: list[str] | None = None) -> tuple[dict, str]:
    bin_ = os.environ.get("TAMARIN_BIN", "tamarin-prover")
    env = dict(os.environ)
    # tamarin's GHC runtime needs a UTF-8 locale to read the .spthy files
    env.setdefault("LC_ALL", "C.UTF-8")
    env.setdefault("LANG", "C.UTF-8")
    proc = subprocess.run(
        [bin_, "--prove", *(extra or []), str(path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    out = proc.stdout + proc.stderr
    results = {m.group(1): m.group(2) for m in SUMMARY_RE.finditer(out)}
    if not results:
        print(out[-4000:])
        raise SystemExit(f"{path.name}: no lemma summary parsed (see output above)")
    return results, out


def main() -> int:
    show_trace = "--trace" in sys.argv
    failures = []
    for fname, expected in EXPECTATIONS.items():
        results, out = run_theory(HERE / fname)
        for lemma, want in expected.items():
            got = results.get(lemma, "MISSING")
            ok = got == want
            mark = "ok " if ok else "FAIL"
            print(f"  [{mark}] {fname:26s} {lemma:32s} want={want:9s} got={got}")
            if not ok:
                failures.append((fname, lemma, want, got))
        if show_trace and fname == "VaultRekeyF001.spthy":
            # the derivation section of the falsified lemma is the F-001 trace
            idx = out.find("exclusion_forward")
            print(out[idx : idx + 3000])
    if failures:
        print(f"\nHARNESS FAILED: {len(failures)} expectation(s) violated.")
        if any(want == "falsified" and got == "verified" for _, _, want, got in failures):
            print(
                "NOTE: a calibration lemma VERIFIED where an attack was expected — the\n"
                "model has lost the defended behaviour and the green proof is not\n"
                "trustworthy. Fix the model, do not celebrate."
            )
        return 1
    n = sum(len(v) for v in EXPECTATIONS.values())
    print(
        f"\nAll {n} expectations hold across {len(EXPECTATIONS)} theories: "
        "every green theory proves, every calibration falsifies its headline lemma."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
