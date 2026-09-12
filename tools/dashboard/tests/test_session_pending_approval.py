"""Session discovery uses Central for the operator, with legacy fallback."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('mode', ['central', 'nonoperator', 'failure', 'hidden'])
def test_session_pending_projection_at_endpoint(monkeypatch, mode):
    from tools.dashboard import server, attention_routes, approval_service, org_identity
    from tools.dashboard.dao import approval_requests
    monkeypatch.delenv('DASHBOARD_MOCK', raising=False)
    row = {'tmux_name':'session-1','project':'workspace'}
    monkeypatch.setattr(server.dashboard_db, 'get_session', lambda name: row)
    monkeypatch.setattr(server.dashboard_db, 'reconcile_session_graph_source_id', lambda row: None)
    monkeypatch.setattr(server, '_session_hidden_cross_org', lambda request, row: mode == 'hidden')
    monkeypatch.setattr(server, 'derive_lifecycle_state', lambda row: 'IDLE')
    monkeypatch.setattr(org_identity, 'resolve_session_org', lambda row: {'slug':'autonomy'})
    legacy = {'id':'legacy-1','kind':'link_publish'}
    central = {'id':'central-1','kind':'dashboard_access','attention_id':'recipient-1'}
    old_lookup = Mock(return_value=legacy)
    lookup = Mock(return_value=central, side_effect=RuntimeError('unavailable') if mode == 'failure' else None)
    monkeypatch.setattr(approval_requests, 'pending_for_session', old_lookup)
    monkeypatch.setattr(attention_routes, 'pending_session_approval', lookup)
    monkeypatch.setattr(attention_routes, '_operator_guard', lambda request: object() if mode == 'nonoperator' else None)
    monkeypatch.setattr(approval_service, 'resolve_human_approval_actor', lambda request: 'verified-actor')
    request = SimpleNamespace(path_params={'tmux_name':'session-1'})
    response = asyncio.run(server.api_session_get(request))
    if mode == 'hidden':
        assert response.status_code == 404
        old_lookup.assert_not_called();lookup.assert_not_called()
    else:
        assert response.status_code == 200
        assert json.loads(response.body)['pending_approval'] == (central if mode == 'central' else legacy)
        if mode == 'nonoperator': lookup.assert_not_called()
        else: lookup.assert_called_once_with(row, 'verified-actor')
