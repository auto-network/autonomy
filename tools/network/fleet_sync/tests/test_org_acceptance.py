"""auto-coea3 acceptance on the fleet harness: real scheduler processes,
three operators (three personal fleets on distinct roots, two machines
each), one organization, personas and adopted membership checkpoints
handed to every machine the way the membership half will.

Criteria (bd acceptance field): (1) an org row written on one member's
machine reaches another member's machine through the org hello; (2)
refusals are typed (unit suites) -- here: a member's PERSONAL hello never
crosses and personal rows never leak; (3) a removing membership checkpoint closes the
removed member within 5 s while the others keep syncing; a rekey keeps
the rekeyed member; no scope re-bootstraps; (4) per-peer state for
the org scope survives the membership changes; (5) the personal path is
unchanged. Plus auto-mldvv: every machine's reachability row crosses
once and is never rewritten on stable addresses.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from tools.network.fleet_org_channel import org_state_key
from tools.network.fleet_sync.harness.fleet import HarnessFleet
from tools.network.fleet_sync.harness.org import ORG, HarnessOrg


def test_three_members_sync_the_org_scope_across_fleets(tmp_path: Path, monkeypatch) -> None:
    # Fleet slots bound how many harness fleets run at once on the machine
    # (HarnessFleet._acquire_fleet_slot: two flock files by default). This
    # organization is three two-machine fleets, and each build() took its
    # own slot, so the test raised the pool to three, took the third slot
    # at once, and then held it while it waited for two other scenarios to
    # release theirs: in the 2026-09-19 full runs 12 of its 17 s went to
    # that wait. The organization now takes ONE slot, on the founder's
    # fleet, and the other two member fleets ride on it. Its six worker
    # processes are what two three-machine fleets already put on one pair
    # of slots, so the concurrency bound is kept, and every assertion below
    # is unchanged.
    real_acquire = HarnessFleet._acquire_fleet_slot
    holder: list = []

    def acquire_once(self, slots=None):
        if not holder:
            real_acquire(self, slots)
            holder.append(self)

    monkeypatch.setattr(HarnessFleet, "_acquire_fleet_slot", acquire_once)
    org = HarnessOrg(tmp_path / "org", members=3, machines_per_member=2).build()
    try:
        org.start_all()
        # (1) rows cross between members in both directions, and reach a
        # member's SECOND machine through that member's own fleet.
        org.write_org(0, 0, "founder-row", "written by the founder")
        org.write_org(1, 1, "joiner-row", "written by a joiner's second machine")
        org.write_personal(0, 0, "founder-personal", "must stay in the founder's fleet")
        for member in range(3):
            for machine in range(2):
                org.wait(lambda m=member, k=machine: org.has_org(m, k, "founder-row"),
                         timeout=90.0, label=f"founder-row on member {member} machine {machine}")
                org.wait(lambda m=member, k=machine: org.has_org(m, k, "joiner-row"),
                         timeout=90.0, label=f"joiner-row on member {member} machine {machine}")
        org.wait(lambda: org.has_personal(0, 1, "founder-personal"), timeout=60.0,
                 label="personal row on the founder's second machine")
        # (5) personal rows never leave the fleet.
        for member in (1, 2):
            for machine in range(2):
                assert not org.has_personal(member, machine, "founder-personal")
        # auto-mldvv: every machine's reachability row is everywhere, once.
        all_machines = sorted(p for m in range(3) for p in org.machine_pubs(m))

        def rows_everywhere() -> bool:
            return all(
                sorted(org.reachability_rows(m, k)) == all_machines
                for m in range(3) for k in range(2)
            )
        org.wait(rows_everywhere, timeout=90.0, label="reachability rows on every machine")
        stamped = {
            (m, k): {key: row["updated_at"] for key, row in org.reachability_rows(m, k).items()}
            for m in range(3) for k in range(2)
        }
        # (4) org peer state is keyed by (machine pair, org) everywhere. A
        # machine that started with an EMPTY org database legitimately
        # bootstrapped from its own fleet at first contact; the criterion is
        # that the membership changes below add no new bootstrap.
        for m in range(3):
            for k in range(2):
                states = org.peer_state(m, k)
                assert states, (m, k)
                assert {row[1] for row in states} == {org_state_key(ORG)}
        before_state = {(m, k): org.peer_state(m, k) for m in range(3) for k in range(2)}

        # (3a) a REKEY: member 1's persona is rekeyed; every machine adopts
        # the new member set; member 1 keeps receiving.
        org.rekey(1)
        time.sleep(1.0)
        org.write_org(0, 0, "after-rekey", "written after the rekey")
        for machine in range(2):
            org.wait(lambda k=machine: org.has_org(1, k, "after-rekey"), timeout=60.0,
                     label=f"row after rekey on member 1 machine {machine}")

        # (3b) a REMOVAL: member 2 is removed; within the deadline every
        # remaining machine refuses it; members 0 and 1 keep syncing.
        #
        # The removal reaches each worker when it reloads its config at the
        # start of its next round, so for up to one round after remove()
        # the writer still admits member 2, and a row written in that
        # window is legitimately delivered. The old test wrote at once and
        # then slept, so the "never arrives" assertion below raced the
        # reload. Wait for the condition the assertion needs: each of
        # member 2's machines has had an org-scope pull refused since the
        # removal. Every worker, member 2's included, reloads the same
        # rewritten configs, so once member 2 proves under the new
        # checkpoint (whose member set excludes it) no machine admits it
        # again, and a refusal observed by member 2 is final. Only member
        # 2's own pull log can show this: a refused dial reaches it as a
        # plain close, and the remaining machines dial member 2 only while
        # its addresses are still current, so their logs cannot be
        # required to carry the refusal.
        removed_wall = time.time()
        org.remove(2)

        def refused_since(machine_index: int, since: float) -> bool:
            """Member 2's machine recorded a failed org-scope pull after
            *since* (wall clock, the worker's ``at`` stamp on each terminal
            pull attempt)."""
            path = org.members[2].fleet.root_dir / f"machine-{machine_index}-pulls.jsonl"
            try:
                lines = path.read_text().splitlines()
            except OSError:
                return False
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a line still being written
                if (entry.get("scope") == org.slug
                        and entry.get("direction") == "pull"
                        and entry.get("outcome") == "failed"
                        and float(entry.get("at", 0)) >= since):
                    return True
            return False

        org.wait(
            lambda: all(refused_since(k, removed_wall) for k in range(2)),
            timeout=60.0, label="both of the removed member's machines refused",
        )
        org.write_org(1, 0, "after-removal", "written after the removal")
        org.wait(lambda: org.has_org(0, 1, "after-removal"), timeout=60.0,
                 label="row after removal on the founder's second machine")
        # The removed member kept dialing after the write and was refused
        # again: it had every chance to be served and was not.
        written_wall = time.time()
        org.wait(
            lambda: all(refused_since(k, written_wall) for k in range(2)),
            timeout=60.0, label="the removed member is refused again after the write",
        )
        for machine in range(2):
            assert not org.has_org(2, machine, "after-removal"), machine
        # Its address rows are no longer offered as peers by anyone else:
        # verified rows for a persona outside the adopted set drop out.
        from tools.network.fleet_org_reachability import co_member_addresses
        from tools.network.idkit import KeyPair  # noqa: F401 - type context
        member0_view = co_member_addresses(
            org.members[0].fleet.org_db_path(0, org.slug), org=ORG,
            own_machine_pub=org.machine_pubs(0)[0],
            is_member=lambda p: p in set(org.adopted[org.seq]),
        )
        assert not set(member0_view) & set(org.machine_pubs(2))

        # (4) peer state for the org scope survived both changes: same keys,
        # same state epoch, no scope re-bootstrapped.
        for m in (0, 1):
            for k in range(2):
                after = org.peer_state(m, k)
                assert {row[0] for row in after} >= {row[0] for row in before_state[(m, k)]}
                assert {row[1] for row in after} == {org_state_key(ORG)}
        # auto-mldvv: no reachability row was rewritten on stable addresses.
        for m in (0, 1):
            for k in range(2):
                now = {key: row["updated_at"] for key, row in org.reachability_rows(m, k).items()}
                for key, stamp in stamped[(m, k)].items():
                    assert now.get(key) == stamp, (m, k, key)
    finally:
        org.shutdown()
