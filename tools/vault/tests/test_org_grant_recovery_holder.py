"""The live holder recovers addressed late grants, respecting declared home."""
from tools.graph import schemas
from tools.vault import key_holder, unlock
from tools.vault.tests.test_unlock import _one_grant


def test_provider_is_live_personal_home_isolated_and_opened_states_are_not_repeated(monkeypatch):
    grant, private, descriptor, secret = _one_grant()
    available = []
    keys = {}
    provider_calls = []
    class Store:
        def __init__(self, _path): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        states = {descriptor.state_id: descriptor}
        def accepted_grants(self): return available
        def accepted_bridges(self): return ()
    monkeypatch.setattr(key_holder, 'KeyControlStore', Store)
    monkeypatch.setattr(key_holder, '_scoped_db', lambda *_: ':memory:')
    monkeypatch.setattr(schemas, 'declared_home', lambda set_id: 'personal' if set_id == 'personal-set' else 'org')
    def provider(genesis):
        provider_calls.append(genesis)
        return keys.get(genesis, {})
    cache = key_holder.VaultKeyCache()
    holder = key_holder.build_key_holder(cache, organization_kem_provider=provider)
    holder(set_id='org-set', org='local-slug')
    assert not cache.secrets
    keys[descriptor.genesis_id] = {grant.recipient_kem_key_id: private}
    holder(set_id='org-set', org='local-slug')
    assert not cache.secrets  # KEM available, grant not received yet.
    available.append(grant)
    previous_calls = list(provider_calls)
    holder(set_id='personal-set', org='local-slug')
    assert provider_calls == previous_calls
    assert not cache.secrets  # Personal home never consumes an org provider.
    opened = []
    original = unlock.open_generation_keys
    def record(*args):
        opened.append(True)
        return original(*args)
    monkeypatch.setattr(unlock, 'open_generation_keys', record)
    holder(set_id='org-set', org='renamed-local-slug')
    assert cache.secrets == {descriptor.state_id: secret}
    holder(set_id='org-set', org='renamed-local-slug')
    assert len(opened) == 1
