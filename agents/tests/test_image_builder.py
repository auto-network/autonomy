"""agents.image_builder — the Settings-driven workspace image builder.

Contract under test: the image name is f(org, key) and nothing else;
builds are content-hash gated against the machine-homed status row; a
disk Dockerfile beside a provision row is a loud collision, never a
silent precedence; and every outcome writes exactly one of digest/error.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents import image_builder, workspace_settings

SHA_OF = lambda text: __import__("hashlib").sha256(text.encode()).hexdigest()


class FakeDocker:
    """Records docker invocations; scripted to fail builds on demand."""

    def __init__(self, fail_build: bool = False):
        self.calls: list[list[str]] = []
        self.fail_build = fail_build

    def __call__(self, cmd, capture_output, text, timeout):
        self.calls.append(cmd)
        if cmd[1] == "build" and self.fail_build:
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="pull access denied")
        if cmd[1] == "image":
            return SimpleNamespace(returncode=0, stdout="sha256:feed\n",
                                   stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def stores(monkeypatch):
    """In-memory provision rows + status rows standing in for ops."""
    state = SimpleNamespace(provision={}, status={})

    state.workspace = {}

    def read_set_key(set_id, key, *, org, peers):
        assert peers == []
        if set_id == workspace_settings.PROVISION_SET_ID:
            payload = state.provision.get((org, key))
        elif set_id == "autonomy.workspace":
            payload = state.workspace.get((org, key))
        else:
            assert set_id == image_builder.IMAGE_BUILD_SET_ID
            assert org == "machine", "status rows live in the machine store"
            payload = state.status.get(key)
        return {"key": key, "payload": payload} if payload else None

    def upsert_by_key(set_id, revision, key, payload, *, org):
        assert (set_id, org) == (image_builder.IMAGE_BUILD_SET_ID, "machine")
        state.status[key] = payload
        return "id-" + key

    for mod in (workspace_settings.ops, image_builder.ops):
        monkeypatch.setattr(mod, "read_set_key", read_set_key)
    monkeypatch.setattr(image_builder.ops, "upsert_by_key", upsert_by_key)
    return state


def test_image_name_is_org_slash_workspace():
    assert image_builder.derive_image_name("anchore", "enterprise-ng") \
        == "anchore/enterprise-ng"


def test_no_dockerfile_is_a_non_event(stores, tmp_path):
    stores.provision[("autonomy", "dev")] = {"startup_script": "echo hi"}
    assert image_builder.build_workspace(
        "autonomy", "dev", repo_root=tmp_path) is None


def test_build_success_writes_digest_row(stores, tmp_path):
    stores.provision[("anchore", "enterprise-ng")] = {"dockerfile": "FROM a"}
    docker = FakeDocker()
    result = image_builder.build_workspace(
        "anchore", "enterprise-ng", repo_root=tmp_path, runner=docker)
    assert result.action == "built"
    assert result.image == "anchore/enterprise-ng"
    build_cmd = docker.calls[0]
    assert build_cmd[:2] == ["docker", "build"]
    assert "anchore/enterprise-ng" in build_cmd
    row = stores.status["anchore:enterprise-ng"]
    assert row["digest"] == "sha256:feed"
    assert row["content_hash"] == SHA_OF("FROM a")
    assert "error" not in row


def test_unchanged_hash_skips_docker_entirely(stores, tmp_path):
    stores.provision[("anchore", "ng")] = {"dockerfile": "FROM a"}
    stores.status["anchore:ng"] = {
        "content_hash": SHA_OF("FROM a"), "digest": "sha256:old",
        "built_at": "x",
    }
    docker = FakeDocker()
    result = image_builder.build_workspace(
        "anchore", "ng", repo_root=tmp_path, runner=docker)
    assert result.action == "skipped"
    assert docker.calls == []


def test_force_rebuilds_unchanged(stores, tmp_path):
    stores.provision[("anchore", "ng")] = {"dockerfile": "FROM a"}
    stores.status["anchore:ng"] = {
        "content_hash": SHA_OF("FROM a"), "digest": "sha256:old",
        "built_at": "x",
    }
    result = image_builder.build_workspace(
        "anchore", "ng", repo_root=tmp_path, force=True, runner=FakeDocker())
    assert result.action == "built"


def test_failed_build_writes_error_row(stores, tmp_path):
    stores.provision[("anchore", "ng")] = {"dockerfile": "FROM nope"}
    result = image_builder.build_workspace(
        "anchore", "ng", repo_root=tmp_path, runner=FakeDocker(fail_build=True))
    assert result.action == "failed"
    row = stores.status["anchore:ng"]
    assert "pull access denied" in row["error"]
    assert "digest" not in row


def test_disk_collision_fails_loudly_without_building(stores, tmp_path):
    stores.provision[("anchore", "ng")] = {"dockerfile": "FROM a"}
    disk = tmp_path / "agents" / "projects" / "ng"
    disk.mkdir(parents=True)
    (disk / "Dockerfile").write_text("FROM disk")
    docker = FakeDocker()
    result = image_builder.build_workspace(
        "anchore", "ng", repo_root=tmp_path, runner=docker)
    assert result.action == "collision"
    assert docker.calls == []
    assert "move-then-delete" in stores.status["anchore:ng"]["error"]


def test_personal_dockerfile_shadows_org(stores, tmp_path):
    stores.provision[("anchore", "ng")] = {"dockerfile": "FROM org"}
    stores.provision[("personal", "ng")] = {"dockerfile": "FROM mine"}
    result = image_builder.build_workspace(
        "anchore", "ng", repo_root=tmp_path, runner=FakeDocker())
    assert stores.status["anchore:ng"]["content_hash"] == SHA_OF("FROM mine")
    assert result.action == "built"


def test_built_result_reports_workspace_image_drift(stores, tmp_path):
    stores.provision[("anchore", "ng")] = {"dockerfile": "FROM a"}
    stores.workspace[("anchore", "ng")] = {"image": "autonomy-session-dind"}
    result = image_builder.build_workspace(
        "anchore", "ng", repo_root=tmp_path, runner=FakeDocker())
    assert "DRIFT" in result.detail
    assert "autonomy-session-dind" in result.detail


def test_sweep_covers_every_org_row(stores, tmp_path, monkeypatch):
    stores.provision[("autonomy", "a")] = {"dockerfile": "FROM x"}
    stores.provision[("anchore", "b")] = {"dockerfile": "FROM y"}
    stores.provision[("anchore", "c")] = {"startup_script": "no image"}

    def read_set(set_id, *, org, peers):
        assert peers == []
        members = [SimpleNamespace(key=key)
                   for (o, key) in stores.provision if o == org]
        return SimpleNamespace(members=members)

    monkeypatch.setattr(image_builder.ops, "read_set", read_set)
    monkeypatch.setattr(image_builder.cross_org, "list_org_slugs",
                        lambda: ["autonomy", "anchore"])
    results = image_builder.sweep(repo_root=tmp_path, runner=FakeDocker())
    assert sorted(r.image for r in results) == ["anchore/b", "autonomy/a"]
