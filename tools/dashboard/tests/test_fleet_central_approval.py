"""Exercise Fleet's Central producer, signed decisions and attention projection."""
from dataclasses import replace
from types import SimpleNamespace
import time
import uuid

import pytest

from tools.dashboard import fleet_enrollment_approvals as fleet
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import ApprovalService, ApprovalServiceError, HumanApprovalActor, InMemoryApprovalStore
from tools.dashboard.attention_index_service import AttentionIndexService, InMemoryAttentionIndexStore
from tools.dashboard.attention_registry import build_production_attention_registry
from tools.dashboard.fleet_enrollment_service import FleetEnrollmentStore
from tools.network import fleet_enroll, fleet_invite, fleet_roster
from tools.network.idkit import KeyPair


@pytest.fixture
def ceremony(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB
    monkeypatch.setenv('AUTONOMY_ORGS_DIR', str(tmp_path / 'orgs'))
    monkeypatch.delenv('GRAPH_DB', raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db('personal', type_='personal').close()
    GraphDB.create_org_db('machine', type_='personal').close()
    now = int(time.time() * 1000)
    root = KeyPair.from_private_hex('34' * 32)
    invite = fleet_invite.mint(root, rendezvous='https://relay.auto.network/l/' + '12' * 16,
                              invite_id='56' * 32, expires_at=now + 60000)
    store = FleetEnrollmentStore(tmp_path / 'machine.db')
    target = str(uuid.uuid4())
    store.register_invite(target_uuid=target, grant_token='12' * 16, invite=invite, now_ms=now)
    request = fleet_enroll.build_request(invite=invite, machine_id='78' * 32)
    pending, _ = store.open_request(target, request, now_ms=now)
    monkeypatch.setattr(fleet, '_store', lambda: store)
    monkeypatch.setattr(fleet, '_anchor', lambda: root.public_hex)
    monkeypatch.setattr(fleet, '_needs_local_bootstrap', lambda: False)
    registry = build_production_registry(runtimes={fleet.KIND: fleet.build_approval_runtime()})
    approvals = ApprovalService(registry=registry, store=InMemoryApprovalStore(),
                                personal_root_resolver=lambda: root.public_hex)
    attention_registry = build_production_attention_registry(
        approval_registry=registry,
        runtimes={(fleet.KIND, 'fleet'): fleet.build_attention_runtime(approvals)},
    )
    index = AttentionIndexService(registry=attention_registry, store=InMemoryAttentionIndexStore())
    monkeypatch.setattr(fleet, '_runtime', lambda: SimpleNamespace(approvals=approvals, index=index))
    aid = fleet.ensure_approval(pending, store=store)
    entry = fleet_roster.enroll(root, machine_id=fleet_enroll.assigned_machine_id(request),
                               machine_pub=KeyPair.generate().public_hex, issued_at=now)
    draft = fleet_enroll.approval_draft(request, invite=invite,
                                      channel_binding=pending.channel_binding, roster_entry=entry)
    approval = replace(draft, signature=root.sign_hex(draft.signing_input()))
    decision = {'machine_name': 'Studio Mac', 'approval': approval.to_dict(), 'roster_entry': entry.to_dict()}
    return SimpleNamespace(root=root, pending=pending, store=store, approvals=approvals,
                           index=index, aid=aid, decision=decision)


def test_request_reuses_one_central_record_and_publishes_review(ceremony):
    c = ceremony
    assert fleet.ensure_approval(c.pending, store=c.store) == c.aid
    assert c.store.get_request(c.pending.request_id).source_approval_id == c.aid
    item = c.index.store.get_item(c.aid)
    assert item.payload['attention_state'] == 'needs_attention'
    assert c.approvals.status(c.aid).request.payload['safe_review']['verification_code'] == c.pending.verification_code


def test_decline_is_only_a_central_decision(ceremony):
    c = ceremony
    c.approvals.decide(c.aid, HumanApprovalActor._verified(c.root.public_hex), outcome='declined', decision={})
    fleet.reconcile(c.aid)
    assert fleet.decision_status(c.aid) == 'declined'
    assert c.store.get_request(c.pending.request_id).status == 'pending'
    assert c.index.store.get_item(c.aid).payload['attention_state'] == 'resolved'


def test_public_signature_is_accepted_and_first_answer_wins(ceremony):
    c = ceremony
    actor = HumanApprovalActor._verified(c.root.public_hex)
    first = c.approvals.decide(c.aid, actor, outcome='granted', decision=c.decision)
    second = c.approvals.decide(c.aid, actor, outcome='declined', decision={})
    assert first.payload == second.payload
    assert c.approvals.store.resolution_count(c.aid) == 1


@pytest.mark.parametrize('extra', ['local_runtime', 'process_private_seed'])
def test_private_runtime_is_not_accepted_as_approval_data(ceremony, extra):
    c = ceremony
    with pytest.raises(ApprovalServiceError, match='invalid_decision'):
        c.approvals.decide(c.aid, HumanApprovalActor._verified(c.root.public_hex),
                           outcome='granted', decision={**c.decision, extra: 'secret'})
    assert c.approvals.status(c.aid).resolution is None


def test_wrong_signature_does_not_commit(ceremony):
    c = ceremony
    decision = {**c.decision, 'approval': {**c.decision['approval'], 'signature': '00' * 64}}
    with pytest.raises(ApprovalServiceError, match='invalid_decision'):
        c.approvals.decide(c.aid, HumanApprovalActor._verified(c.root.public_hex), outcome='granted', decision=decision)
    assert c.approvals.status(c.aid).resolution is None


def test_grant_executes_real_roster_commit_and_projects_success(ceremony):
    c = ceremony
    c.approvals.decide(c.aid, HumanApprovalActor._verified(c.root.public_hex), outcome='granted', decision=c.decision)
    fleet.reconcile(c.aid)
    assert c.store.get_request(c.pending.request_id).status == 'approved'
    entries = fleet_roster.load_entries(org=None)
    assert len(entries) == 1
    assert entries[0].machine_id == c.pending.request.machine_id
    assert fleet.project_result(c.approvals.status(c.aid))['execution']['ok'] is True
    fleet.reconcile(c.aid)
    assert len(fleet_roster.load_entries(org=None)) == 1


def test_standalone_conversion_commits_both_public_roster_entries(ceremony, monkeypatch):
    from tools.network import machine_boot
    c = ceremony
    monkeypatch.setattr(fleet, '_needs_local_bootstrap', lambda: True)
    with c.store._connect() as conn:
        invite = c.store._invite_row(conn, c.pending.target_uuid, int(time.time() * 1000))
    request = fleet_enroll.build_request(invite=invite, machine_id='89' * 32)
    pending, _ = c.store.open_request(c.pending.target_uuid, request)
    aid = fleet.ensure_approval(pending, store=c.store)
    staged = c.approvals.get_request(aid).payload['staged']
    local = fleet_roster.enroll(c.root, machine_id=staged['local_bootstrap_machine_id'],
                               machine_pub=KeyPair.generate().public_hex)
    remote = fleet_roster.enroll(c.root, machine_id=request.machine_id,
                                machine_pub=KeyPair.generate().public_hex)
    draft = fleet_enroll.approval_draft(request, invite=invite, channel_binding=pending.channel_binding,
                                       roster_entry=remote)
    signed = replace(draft, signature=c.root.sign_hex(draft.signing_input()))
    decision = {'machine_name': 'New Mac', 'approval': signed.to_dict(),
                'roster_entry': remote.to_dict(), 'local_roster_entry': local.to_dict()}
    c.approvals.decide(aid, HumanApprovalActor._verified(c.root.public_hex), outcome='granted', decision=decision)
    fleet.reconcile(aid)
    assert c.store.get_request(pending.request_id).status == 'approved'
    assert machine_boot.machine_id(org='machine') == local.machine_id
    assert {entry.machine_id for entry in fleet_roster.load_entries(org=None)} == {local.machine_id, remote.machine_id}
    assert c.approvals.status(aid).resolution.payload['decision'] == decision


def test_roster_write_failure_is_not_reported_as_success(ceremony, monkeypatch):
    c = ceremony
    def fail_write(*args, **kwargs):
        raise OSError('roster write unavailable')
    monkeypatch.setattr(c.store, 'approve', fail_write)
    c.approvals.decide(c.aid, HumanApprovalActor._verified(c.root.public_hex),
                       outcome='granted', decision=c.decision)
    fleet.reconcile(c.aid)
    assert c.store.get_request(c.pending.request_id).status == 'failed'
    assert fleet.project_result(c.approvals.status(c.aid)) == {
        'execution': {'ok': False, 'error': 'approval_execution_failed'},
    }
