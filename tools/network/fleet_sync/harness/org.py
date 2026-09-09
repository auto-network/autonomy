"""Two operators, one organization: the fleet harness taught personas and
adopted membership checkpoints (auto-coea3 acceptance).

Each member is a HarnessFleet of its own -- its own personal root, roster,
machines, hub -- exactly as two people's fleets are. The organization is a
genesis id, one persona per member, and a sequence of adopted membership
checkpoints (seq -> member persona set) that this parent hands every
machine through its worker config, as the membership half will hand a
real node. Seams lifted from tools/dashboard/tests/membership_sim/_harness.py:
membership_commitment.inclusion_proof / compute_root for riders and roots,
the issue_cert persona pattern for the machine certificate.

Cross-member dials go to real listener ports (no fault hub between
fleets); first contact is one address of the founder's machine 0 handed
to the joiner's machines, and everything after rides the org hello's
introduction and the replicated reachability rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from tools.network.idkit import KeyPair, Subject, issue_cert

from .fleet import HarnessFleet

ORG = "genesis-" + "ab" * 28
DAY = 86_400


def _machine_cert(persona: KeyPair, machine_pub: str, org: str) -> dict:
    now = int(time.time())
    return issue_cert(
        persona, machine_pub, scope=("fleet:sync",), org=org,
        subject=Subject("persona", persona.public_hex),
        not_before=now - 300, not_after=now + 30 * DAY,
    ).to_dict()


@dataclass
class MemberFleet:
    """One member: their persona and their fleet."""

    name: str
    persona: KeyPair
    fleet: HarnessFleet
    certs: dict[int, dict] = field(default_factory=dict)  # machine index -> cert


class HarnessOrg:
    """Build and drive an organization of member fleets."""

    def __init__(
        self, root_dir: Path, *, members: int = 2, machines_per_member: int = 2,
        slug: str = "alpha", org: str = ORG, poll_interval: float = 0.1,
    ) -> None:
        if members < 2:
            raise ValueError("an organization scenario needs at least two members")
        self.root_dir = Path(root_dir)
        self.slug = slug
        self.org = org
        self.poll_interval = poll_interval
        self.members: list[MemberFleet] = []
        for index in range(members):
            persona = KeyPair.generate()
            fleet = HarnessFleet(
                self.root_dir / f"member-{index}", machines_per_member,
                seed=7 + index, org_scopes=(slug,), poll_interval=poll_interval,
                max_concurrent_pulls=1,
            )
            self.members.append(MemberFleet(f"member-{index}", persona, fleet))
        #: adopted membership checkpoints: seq -> member persona pubs.
        self.adopted: dict[int, list[str]] = {0: [m.persona.public_hex for m in self.members]}
        self.seq = 0
        #: machine configs are written by HarnessFleet; this parent layers
        #: the org block on top through the fleet's config hook.

    # -- construction -----------------------------------------------------

    def build(self) -> "HarnessOrg":
        for member in self.members:
            member.fleet.build()
            for machine in member.fleet.machines:
                member.certs[machine.index] = _machine_cert(
                    member.persona, machine.key.public_hex, self.org,
                )
            member.fleet.config_extra = lambda machine, member=member: self._org_block(member, machine)
            member.fleet.worker_env = lambda machine, member=member: {
                # The worker's org database is the settings home for the
                # slug, so its reachability row lands in the store it syncs.
                "AUTONOMY_ORGS_DIR": str(member.fleet.org_db_path(machine.index, self.slug).parent),
            }
        return self

    def _org_block(self, member: MemberFleet, machine) -> dict:
        first_contact: dict[str, dict[str, list[str]]] = {}
        # First contact: every member other than the founder gets the
        # founder's machine 0 address once it is up; the founder learns
        # the others from their org hellos.
        founder = self.members[0]
        if member is not founder and founder.fleet.machines[0].port:
            first_contact[self.slug] = {
                founder.fleet.machines[0].key.public_hex: [
                    f"ws://127.0.0.1:{founder.fleet.machines[0].port}"
                ],
            }
        return {
            "org_channels": {
                self.slug: {
                    "org": self.org,
                    "persona_cert": member.certs[machine.index],
                    "adopted": {str(seq): list(pubs) for seq, pubs in self.adopted.items()},
                    "seq": self.seq,
                },
            },
            "org_peer_addresses": first_contact,
        }

    # -- lifecycle --------------------------------------------------------

    def start_all(self) -> None:
        for member in self.members:
            member.fleet.start_all()

    def shutdown(self) -> None:
        for member in reversed(self.members):
            member.fleet.shutdown()

    def rewrite_configs(self) -> None:
        """Hand every running machine the current adopted checkpoints (and
        first contacts): the worker reloads its config on the next round."""
        for member in self.members:
            for machine in member.fleet.machines:
                member.fleet._write_config(machine)

    # -- membership events (root ceremonies, adopted everywhere) ------------

    def adopt(self, pubs: list[str]) -> int:
        self.seq += 1
        self.adopted[self.seq] = list(pubs)
        self.rewrite_configs()
        return self.seq

    def remove(self, member_index: int) -> int:
        remaining = [
            m.persona.public_hex for i, m in enumerate(self.members) if i != member_index
        ]
        return self.adopt(remaining)

    def rekey(self, member_index: int) -> int:
        member = self.members[member_index]
        member.persona = KeyPair.generate()
        for machine in member.fleet.machines:
            member.certs[machine.index] = _machine_cert(
                member.persona, machine.key.public_hex, self.org,
            )
        return self.adopt([m.persona.public_hex for m in self.members])

    # -- data ---------------------------------------------------------------

    def write_org(self, member_index: int, machine_index: int, source_id: str, title: str) -> None:
        self.members[member_index].fleet.write_org(machine_index, self.slug, source_id, title)

    def has_org(self, member_index: int, machine_index: int, source_id: str) -> bool:
        return self.members[member_index].fleet.has_org(machine_index, self.slug, source_id)

    def write_personal(self, member_index: int, machine_index: int, source_id: str, title: str) -> None:
        self.members[member_index].fleet.write(machine_index, source_id, title)

    def has_personal(self, member_index: int, machine_index: int, source_id: str) -> bool:
        return self.members[member_index].fleet.has(machine_index, source_id)

    def wait(self, predicate, *, timeout: float, label: str) -> float:
        started = time.monotonic()
        deadline = started + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out waiting for {label}")
            time.sleep(0.05)
        return time.monotonic() - started

    def reachability_rows(self, member_index: int, machine_index: int) -> dict:
        from tools.network.fleet_org_reachability import read_rows

        return read_rows(self.members[member_index].fleet.org_db_path(machine_index, self.slug))

    def peer_state(self, member_index: int, machine_index: int) -> list[tuple]:
        path = self.members[member_index].fleet.org_db_path(machine_index, self.slug)
        with sqlite3.connect(path) as conn:
            return conn.execute(
                "SELECT machine_public_key,roster_epoch,"
                "COALESCE(last_success_ns,0) FROM fleet_sync_peer_state "
                "ORDER BY machine_public_key"
            ).fetchall()

    def machine_pubs(self, member_index: int) -> list[str]:
        return [m.key.public_hex for m in self.members[member_index].fleet.machines]

    def settings_write_count(self, member_index: int, machine_index: int) -> int:
        """Catalog rows for the reachability set in this machine's org store:
        how many times a reachability row was written here (each write is
        one captured operation)."""
        path = self.members[member_index].fleet.org_db_path(machine_index, self.slug)
        with sqlite3.connect(path) as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM fleet_sync_catalog WHERE address LIKE ?",
                ('%autonomy.org.fleet-reachability%',),
            ).fetchone()[0])
