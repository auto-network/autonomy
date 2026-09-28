"""auto-0skxh: persistent profile paths are built, never supplied."""

import pytest

from tools.browser_broker.profiles import (
    profile_container_name,
    profile_mount,
    profile_path,
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_PROFILES_DIR", str(tmp_path / "browser-profiles"))
    return (tmp_path / "browser-profiles").resolve()


@pytest.mark.parametrize("name", [
    "..", "../../other-workspace/x", "...", "a/b", "A", "a" * 65, "", ".hidden", "a\0b",
])
def test_profile_path_refuses_bad_names(root, name):
    with pytest.raises(ValueError):
        profile_path("autonomy", "personal-finance", name)


@pytest.mark.parametrize("org,workspace", [
    ("..", "ws"), ("autonomy", ".."), ("a/b", "ws"), ("autonomy", ""), ("", "ws"),
])
def test_profile_path_refuses_bad_scope_segments(root, org, workspace):
    with pytest.raises(ValueError):
        profile_path(org, workspace, "eversource")


def test_profile_path_is_inside_its_workspace(root):
    path = profile_path("autonomy", "personal-finance", "eversource")
    assert path == root / "autonomy" / "personal-finance" / "eversource"
    assert path.parent == root / "autonomy" / "personal-finance"
    assert profile_path("autonomy", "ws", "a" * 64).name == "a" * 64


def test_profile_path_refuses_a_symlink_out_of_the_workspace(root, tmp_path):
    base = root / "autonomy" / "ws"
    base.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (base / "eversource").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        profile_path("autonomy", "ws", "eversource")


@pytest.mark.parametrize("level", ["org", "workspace"])
def test_profile_path_refuses_a_symlinked_org_or_workspace(root, tmp_path, level):
    (tmp_path / "elsewhere").mkdir()
    root.mkdir(parents=True)
    if level == "org":
        (root / "autonomy").symlink_to(tmp_path / "elsewhere")
    else:
        (root / "autonomy").mkdir()
        (root / "autonomy" / "ws").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        profile_path("autonomy", "ws", "eversource")


def test_container_name_is_stable_and_distinct():
    name = profile_container_name("autonomy", "ws", "eversource")
    assert name == profile_container_name("autonomy", "ws", "eversource")
    assert name.startswith("brw-p-") and len(name) == len("brw-p-") + 16
    others = {
        profile_container_name("other", "ws", "eversource"),
        profile_container_name("autonomy", "ws2", "eversource"),
        profile_container_name("autonomy", "ws", "bank"),
    }
    assert name not in others and len(others) == 3


def test_container_name_validates_like_the_path():
    with pytest.raises(ValueError):
        profile_container_name("autonomy", "ws", "../x")


def test_profile_mount_targets_slash_profile(root):
    spec = profile_mount("autonomy", "ws", "eversource")
    assert spec.source == str(profile_path("autonomy", "ws", "eversource"))
    assert spec.dest == "/profile"
