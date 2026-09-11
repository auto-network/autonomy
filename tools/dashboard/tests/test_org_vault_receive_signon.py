"""Two-node functional simulation: receive persisted records, sign in, read.

Transport is simulated with consistent SQLite backups into a second data root;
this tests sign-in recovery, not fleet replication. The receiver is a fresh
Python interpreter and runs the real JavaScript ceremony via real handlers.
Only the user's root crosses its input pipe, as browser sign-in input. No KEM
private key, generation key, manual grant opening or replacement write is used.
Authentication itself and unrelated fleet/certificate work remain fixtures.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest


def _reload_receiver():
    """Fresh worker: only the production carrier files, no root or key pipe."""
    from tools.dashboard import unlock_routes
    from tools.graph import settings_ops
    from tools.network.idkit import KeyPair
    data = json.loads(sys.stdin.readline())
    assert not unlock_routes._VAULT_CACHE
    with pytest.MonkeyPatch.context() as mp:
        # Same filesystem fixture as test_vault_hot_reload. Production's RAM
        # guard is tested separately; serialization and restoration are real.
        mp.setattr('tools.network.storagekit.memory_cache.assert_memory_backed', lambda *_: None)
        assert unlock_routes.restore_vault_across_hot_reload()
        row = settings_ops.read_set_key('autonomy.network.link-channel-key', data['token'], org=data['org'])
        seed = (row or {}).get('payload', {}) or {}
        print(json.dumps({'opened': bool(seed.get('seed')) and
              KeyPair.from_private_hex(seed['seed']).public_hex == data['public']}), flush=True)


def _receiver():
    from types import SimpleNamespace
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from tools.dashboard.tests import test_org_vault_signon_write as scenario
    from tools.dashboard import (fleet_enrollment_routes as fleet, identity_routes,
        vault_routes, link_serving_supervisor, service_certificate_manager)
    from tools.network.storagekit.keycontrol import KeyControlStore
    from tools.vault.db_content_store import vault_db_path_for

    inputs = json.loads(sys.stdin.readline())
    mode = inputs.get('mode', 'signin')
    org = inputs['terms']['org']
    assert not scenario.unlock_routes._VAULT_CACHE
    assert scenario.settings_ops._personal_delegate_audited_key is None
    with KeyControlStore(vault_db_path_for(org)) as kc:
        before = (len(kc.states), len(kc.accepted_grants()),
                  kc.db.execute('SELECT COUNT(*) FROM keycontrol_credential').fetchone()[0])
        assert before[0] > 0 and before[1] > 0
    with scenario.LedgerStore(scenario.org_ledger_db_path(org)) as ledger:
        events_before = ledger.ledger.all_ids()
    async def report(request):
        return JSONResponse({'ok': True})
    async def unexpected_checkpoint(request):
        raise AssertionError('receiver should reuse the adopted checkpoint')
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scenario.unlock_routes, 'session_from_request', lambda r: {'sid': 'receiver'})
        if mode == 'reload':
            carrier = Path(os.environ['AUTONOMY_DATA_ROOT']) / 'keycache'
            carrier.mkdir()
            mp.setenv('AUTONOMY_KEYCACHE_MOUNT', str(carrier))
            mp.setattr('tools.network.storagekit.memory_cache.assert_memory_backed', lambda *_: None)
        else:
            mp.setattr(scenario.unlock_routes, 'save_vault_across_hot_reload', lambda: False)
        mp.setattr(service_certificate_manager, 'request_reconcile', lambda: None)
        mp.setattr(identity_routes, '_personal_member',
            lambda: SimpleNamespace(payload={'root_pub': inputs['root_pub']}))
        mp.setattr(vault_routes, 'root_anchor_inventory', lambda: {
            'anchors': [], 'classes': [{'governance': {'form': 'root-reachable'}}]})
        mp.setattr(fleet, 'runtime_preparation', lambda: JSONResponse({'enabled': False}))
        mp.setattr(fleet, '_local_completion_state', lambda: (None, None, None))
        mp.setattr(link_serving_supervisor, 'serve_cert_state', lambda org: {})
        mp.setattr(link_serving_supervisor, 'serve_cert_requirement', lambda org: {'required': False})
        app = Starlette(routes=[
            Route('/api/identity/unlock/preparation', scenario.signon_preparation.get_preparation),
            Route('/api/identity/unlock/vault-keys', scenario.unlock_routes.post_unlock_vault_keys, methods=['POST']),
            Route('/api/network/unlock-report', report, methods=['POST']),
            Route('/api/identity/ceremony-error', report, methods=['POST']),
            Route('/api/network/membership-checkpoint', unexpected_checkpoint, methods=['POST']),
        ])
        with pytest.MonkeyPatch.context() as delivery:
            if mode == 'late':
                # Simulate grant availability arriving later than the other
                # replicated records; the next read uses the real store again.
                delivery.setattr(KeyControlStore, 'accepted_grants', lambda self: ())
            with TestClient(app) as client:
                scenario._run_ceremony(client, inputs['terms'])
            if mode == 'late':
                unavailable = scenario.settings_ops.read_set_key(
                    'autonomy.network.link-channel-key', inputs['token'], org=org)
                assert not (unavailable.get('payload') or {}).get('seed')
                assert scenario.unlock_routes._VAULT_CACHE['organization_kem_keys']
        signing = scenario.org_storage_delegate.signing_key(org)
        row = scenario.settings_ops.read_set_key(
            'autonomy.network.link-channel-key', inputs['token'], org=org)
        payload = (row or {}).get('payload') or {}
        opened = False
        if payload.get('seed'):
            opened = scenario.KeyPair.from_private_hex(payload['seed']).public_hex == inputs['public']
        if mode == 'reload':
            proc = subprocess.run([sys.executable, '-c',
                'from tools.dashboard.tests.test_org_vault_receive_signon import _reload_receiver; _reload_receiver()'],
                input=json.dumps({'org': org, 'token': inputs['token'], 'public': inputs['public']}) + '\n',
                text=True, capture_output=True, timeout=30)
            assert proc.returncode == 0, proc.stderr
            opened = opened and json.loads(proc.stdout.strip().splitlines()[-1])['opened']
        with KeyControlStore(vault_db_path_for(org)) as kc:
            after = (len(kc.states), len(kc.accepted_grants()),
                     kc.db.execute('SELECT COUNT(*) FROM keycontrol_credential').fetchone()[0])
        with scenario.LedgerStore(scenario.org_ledger_db_path(org)) as ledger:
            events_after = ledger.ledger.all_ids()
        print(json.dumps({'signing_delegate_open': signing is not None,
            'row_present': row is not None, 'opened': opened,
            'vault_error': (row or {}).get('vault_error'),
            'records_unchanged': before == after and events_before == events_after}), flush=True)


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not on PATH')
@pytest.mark.parametrize('mode', ['signin', 'late', 'reload'])
def test_received_org_value_opens_after_real_signin(tmp_path, monkeypatch, mode):
    from tools.dashboard.tests import test_org_vault_signon_write as scenario
    def receive(sender, terms, root_pub, token, public):
        receiver = tmp_path / 'receiver'
        receiver.mkdir()
        # Snapshot only persisted databases. No live cache or RAM handoff files.
        databases = list(sender.rglob('*.db'))
        for source in databases:
            destination = receiver / source.relative_to(sender)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as src:
                with sqlite3.connect(destination) as dst:
                    src.backup(dst)
        env = dict(os.environ, AUTONOMY_DATA_ROOT=str(receiver),
                   AUTONOMY_ORGS_DIR=str(receiver / 'orgs'))
        proc = subprocess.run([sys.executable, '-c',
            'from tools.dashboard.tests.test_org_vault_receive_signon import _receiver; _receiver()'],
            input=json.dumps({'terms': terms, 'root_pub': root_pub, 'mode': mode,
                              'token': token, 'public': public}) + '\n',
            text=True, capture_output=True, env=env, timeout=60)
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result['signing_delegate_open'], result
        assert result['row_present'], result
        assert result['records_unchanged'], result
        assert result['opened'], result
    scenario.test_root_unlock_enables_organization_channel_key_write(
        tmp_path, monkeypatch, unavailable_org=False, has_org_key=False,
        receiver_check=receive)
