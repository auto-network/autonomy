"""D5-9 tests — the credential seam redacts everywhere and reveals only on purpose."""

from __future__ import annotations

import json
import pickle

import pytest

from tools.dashboard.commit_broker.credentials import (
    AuthorizedScope,
    Credential,
    CredentialScopeError,
    InMemoryCredentialProvider,
)

SECRET = "ghp_TOPSECRET_do_not_log_me_123456"


def _scope() -> AuthorizedScope:
    return AuthorizedScope.for_repos("op-1", ["auto-network/autonomy"])


def _cred() -> Credential:
    return Credential(secret=SECRET, provider="github", scope=_scope())


def test_str_repr_format_all_redact():
    cred = _cred()
    assert SECRET not in str(cred)
    assert SECRET not in repr(cred)
    assert SECRET not in f"{cred}"
    assert SECRET not in f"{cred!r}"
    assert SECRET not in "{}".format(cred)
    # provider is fine to show; the secret never is
    assert "github" in repr(cred)


def test_reveal_is_the_only_path_to_the_secret():
    cred = _cred()
    assert cred.reveal() == SECRET


def test_pickle_is_blocked_so_the_secret_cannot_be_smuggled_out():
    cred = _cred()
    with pytest.raises(TypeError):
        pickle.dumps(cred)


def test_json_serialization_never_leaks():
    cred = _cred()
    # Direct json.dumps has no encoder for Credential -> raises, never leaks.
    with pytest.raises(TypeError):
        json.dumps({"credential": cred})
    # Even a permissive default=str only ever sees the redacted form.
    dumped = json.dumps({"credential": cred}, default=str)
    assert SECRET not in dumped


def test_empty_secret_is_rejected():
    with pytest.raises(ValueError):
        Credential(secret="", provider="github", scope=_scope())


def test_authorized_scope_permits_only_in_set():
    scope = AuthorizedScope.for_repos("op-1", ["auto-network/autonomy"])
    assert scope.permits("auto-network/autonomy")
    assert not scope.permits("auto-network/other")


def test_scope_is_a_typed_object_not_a_string():
    # The interface carries the server-resolved object; a raw string is not one.
    scope = _scope()
    assert isinstance(scope, AuthorizedScope)
    assert not isinstance("auto-network/autonomy", AuthorizedScope)


def test_scope_error_names_the_out_of_scope_repo():
    scope = _scope()
    err = CredentialScopeError("auto-network/secret", scope)
    assert err.repo == "auto-network/secret"
    assert "secret" in str(err)


def test_provider_returns_a_redaction_wrapped_credential():
    provider = InMemoryCredentialProvider()
    provider.set_secret("github", SECRET)
    cred = provider.get_real_credential("github", _scope())
    assert isinstance(cred, Credential)
    assert cred.reveal() == SECRET
    assert SECRET not in str(cred)


def test_provider_unknown_provider_raises():
    provider = InMemoryCredentialProvider()
    with pytest.raises(KeyError):
        provider.get_real_credential("github", _scope())
