"""A repository's address decomposes and recomposes without losing anything.

The stored shape is the parts — user, host, path — because the host is what a
credential is keyed by. That is only safe if composing them back yields the
remote you started with: a decomposition that quietly drops the login user, or
the leading slash that makes a path absolute on the server, repoints the
remote at something else and the clone fails somewhere far from here.
"""
from __future__ import annotations

import pytest

from agents.workspace_settings import (
    RepoMount,
    WorkspaceSettingsError,
    split_remote_url,
)


LIVE_FORMS = [
    # The scp-style form, which is nearly everything.
    "git@github.com:anchore/anchorectl.git",
    # An ssh config alias rather than a real domain.
    "git@github-autonomy:auto-network/autonomy.git",
    # A private server: a non-default login user, and a path that is
    # absolute ON THAT SERVER rather than relative to the user's home.
    "ssh://admin@5.161.244.118/opt/git/infra.git",
    # A local-first repository with no remote at all.
    "/srv/mirrors/svn-mirror",
]


@pytest.mark.parametrize("url", LIVE_FORMS)
def test_every_form_in_use_survives_the_round_trip(url):
    assert RepoMount.from_url(url, mount="/w/x").url == url


def test_the_login_user_is_kept():
    user, host, path = split_remote_url("ssh://admin@5.161.244.118/opt/git/infra.git")
    assert (user, host, path) == ("admin", "5.161.244.118", "/opt/git/infra")


def test_an_absolute_path_stays_absolute():
    """`/opt/git/infra` on the server is not `opt/git/infra` under a home
    directory, and the two are different repositories."""
    absolute = RepoMount.from_url("ssh://admin@h/opt/git/infra.git", mount="/w/x")
    relative = RepoMount.from_url("admin@h:opt/git/infra.git", mount="/w/x")

    assert absolute.repo == "/opt/git/infra"
    assert relative.repo == "opt/git/infra"
    assert absolute.url != relative.url


def test_the_default_user_needs_no_field():
    mount = RepoMount.from_url("git@github.com:o/r.git", mount="/w/x")
    assert mount.user in (None, "git")
    assert mount.url == "git@github.com:o/r.git"


def test_a_form_that_would_not_survive_is_refused():
    """Better to refuse than to store something that composes back into a
    different remote."""
    with pytest.raises(WorkspaceSettingsError, match="unrecognized git remote"):
        split_remote_url("https://github.com/foo/bar.git")


def test_a_local_repository_has_no_host_to_key_a_credential_by():
    mount = RepoMount.from_url("/srv/mirrors/svn-mirror", mount="/w/x")
    assert mount.host is None and mount.repo is None
    assert mount.local_path == "/srv/mirrors/svn-mirror"
