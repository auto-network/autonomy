"""What an organization's workspaces still need on THIS machine.

Joining an organization brings its workspace declarations with it. None of
them say anything about the machine that just joined: a workspace names a
repository, the directories it mounts, the credentials it uses and the host
variables it forwards, and every one of those is answered locally or not at
all. Until somebody answers them the workspace is a description of something
that does not run here.

This reports what is still unanswered, per workspace, so that setting one up
is a list of things to do rather than a sequence of failures to interpret.
It is the same walk :func:`settings_ops.check_setting` performs, gathered
across every workspace an organization declares -- so a set that starts
declaring a dependency is covered here without anything being added.

Read-only. Nothing here provisions, writes, or launches.

**Run it where the answers live.** A workspace's paths belong to the machine
running the platform; a container asking about them is looking at a different
filesystem and gets told so rather than guessed at. The frame travels on every
finding for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _dc_field

from tools.graph import settings_ops
from tools.graph.schemas.workspace import WORKSPACE_SET_ID


@dataclass
class WorkspaceReadiness:
    """One workspace, and what stands between it and running here."""

    workspace_id: str
    org: str
    name: str
    #: Unsatisfied requirements that stop it running.
    blocking: list = _dc_field(default_factory=list)
    #: Declared and absent, but it runs anyway — a local clone source whose
    #: absence costs a network fetch, an optional mount.
    advisory: list = _dc_field(default_factory=list)
    #: Questions this process is in the wrong place to answer. Not the same
    #: as satisfied, and reported separately so a check run somewhere
    #: unhelpful cannot read as a clean result.
    unanswerable: list = _dc_field(default_factory=list)
    #: Predicates answered and satisfied during the same traversal. Values
    #: never travel; environment evidence contains names and source only.
    satisfied: list = _dc_field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Nothing blocks it, and nothing was left unasked.

        An unanswerable finding counts against ready. Treating it as
        satisfied is how a check run in a container reports a workspace as
        set up when nobody has looked at the filesystem it actually uses.
        """
        return not self.blocking and not self.unanswerable


def _classify(findings) -> tuple[list, list, list]:
    blocking, advisory, unanswerable = [], [], []
    for finding in findings:
        if finding.kind == "unanswerable_here":
            unanswerable.append(finding)
        elif getattr(finding, "severity", "blocking") == "advisory":
            advisory.append(finding)
        else:
            blocking.append(finding)
    return blocking, advisory, unanswerable


def workspace_readiness(workspace_id: str, *, org: str) -> WorkspaceReadiness:
    """What one workspace still needs here."""
    row = settings_ops.read_set_key(WORKSPACE_SET_ID, workspace_id, org=org)
    payload = (row or {}).get("payload") or {}
    findings, satisfied = settings_ops.inspect_setting(
        WORKSPACE_SET_ID, workspace_id, org=org,
    )
    blocking, advisory, unanswerable = _classify(findings)
    return WorkspaceReadiness(
        workspace_id=workspace_id,
        org=org,
        name=str(payload.get("name") or workspace_id),
        blocking=blocking,
        advisory=advisory,
        unanswerable=unanswerable,
        satisfied=satisfied,
    )


def org_readiness(org: str) -> list[WorkspaceReadiness]:
    """Every workspace this organization declares, in key order.

    Reads the organization's OWN rows. A workspace visible only because a
    peer publishes it is that peer's to set up, and listing it here would
    ask the operator to provision something they do not own.
    """
    members = settings_ops.read_owned_set(WORKSPACE_SET_ID, org=org).members
    return [
        workspace_readiness(str(member.key), org=org)
        for member in sorted(members, key=lambda m: str(m.key))
    ]
