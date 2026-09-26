import json
from types import SimpleNamespace

from tools.network.registry import admin
from tools.network.registry.store import RegistryStore


def test_show_is_read_only_and_missing_db_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    path = tmp_path / "registry.db"
    args = ["--db", str(path), "domain", "show", "anchore.serve.auto.network"]
    assert admin.main(args) == 2 and not path.exists()
    store = RegistryStore(str(path))
    store.close()
    monkeypatch.setattr(admin.os, "geteuid", lambda: path.stat().st_uid)
    assert admin.main(args) == 0
    assert json.loads(capsys.readouterr().out)["available"] is True
    monkeypatch.setattr(admin.os, "geteuid", lambda: path.stat().st_uid + 1)
    assert admin.main(args) == 2


def test_remote_arguments_are_shell_quoted_and_status_propagates(monkeypatch):
    calls = []
    monkeypatch.setattr(admin.subprocess, "run", lambda cmd, **kw:
                        calls.append(cmd) or SimpleNamespace(returncode=7))
    assert admin.main(["--target", "root@registry.auto.network", "domain", "show",
                       "x; touch /tmp/bad"]) == 7
    assert "'x; touch /tmp/bad'" in calls[0][-1]
    assert calls[0][:5] == ["ssh", "-o", "BatchMode=yes", "--", "root@registry.auto.network"]
    assert admin.main(["--target=-oProxyCommand=bad", "domain", "show", "x"]) == 2
    assert len(calls) == 1


def test_deploy_wraps_existing_script(monkeypatch):
    calls = []
    monkeypatch.setattr(admin.subprocess, "run", lambda cmd, **kw:
                        calls.append(cmd) or SimpleNamespace(returncode=0))
    assert admin.main(["deploy"]) == 2
    assert admin.main(["--target", "root@registry.auto.network", "deploy"]) == 0
    assert calls[0][0] == "bash" and calls[0][1].endswith("registry/deploy/deploy.sh")
    assert calls[0][2] == "root@registry.auto.network"


def test_root_commands_use_existing_service_identity(tmp_path, monkeypatch):
    path = tmp_path / "registry.db"
    RegistryStore(str(path)).close()
    original_stat = admin.Path.stat
    monkeypatch.setattr(admin.Path, "stat", lambda self, **kw:
        SimpleNamespace(st_uid=63703, st_mode=original_stat(self, **kw).st_mode))
    monkeypatch.setattr(admin.os, "geteuid", lambda: 0)
    calls = []
    monkeypatch.setattr(admin.subprocess, "run", lambda cmd, **kw:
                        calls.append(cmd) or SimpleNamespace(returncode=0))
    assert admin.main(["--db", str(path), "domain", "reserve", "anchore.serve.auto.network",
                       "--org", "11111111-1111-4111-8111-111111111111"]) == 0
    assert calls[0][:4] == ["systemd-run", "--pipe", "--wait", "--collect"]
    assert "User=autonomy-registry" in calls[0]
    assert "DynamicUser=yes" in calls[0]
    assert "StateDirectory=autonomy-registry" in calls[0]
    assert admin.main(["--db", str(path), "domain", "show", "anchore.serve.auto.network"]) == 0
    assert calls[1][:4] == ["systemd-run", "--pipe", "--wait", "--collect"]
