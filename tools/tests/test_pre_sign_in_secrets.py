"""Node secrets: only those needed before sign-in stay as files, each a
recorded exception (operator ruling 2026-10-01, auto-es7ja)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tools.data_paths import STORE_MANIFEST, STORES_BY_KEY

REPO = Path(__file__).resolve().parents[2]

#: The deliberate exceptions: needed before the operator signs in.
PRE_SIGN_IN = {"dashboard_session_secret", "tls_key", "web_push_keys", "beads"}
#: Node-secret stores the bead still moves out of the manifest (vault row or
#: ramfs) or deletes. Each later landing removes its entry; empty = done.
STILL_TO_MOVE = {"repl_login_key", "web_push_proof_vapid"}


def test_exactly_the_recorded_exceptions_stay_files_and_each_says_why():
    marked = {s.key for s in STORE_MANIFEST if s.pre_sign_in}
    assert marked == PRE_SIGN_IN
    for key in marked:
        assert len(STORES_BY_KEY[key].pre_sign_in) > 20, key


def test_every_node_secret_store_is_an_exception_or_on_its_way_out():
    """A secret store (key/secret/credential/VAPID in its name or relative
    path) is either a recorded pre-sign-in exception or still listed to
    move; nothing else may keep a secret on disk."""
    words = ("key", "secret", "vapid", "credential", "beads")
    secret_stores = {s.key for s in STORE_MANIFEST
                     if any(w in (s.key + s.relative).lower() for w in words)
                     and s.key not in ("serving_keys",)}   # holds no key material
    assert secret_stores <= PRE_SIGN_IN | STILL_TO_MOVE, secret_stores - PRE_SIGN_IN - STILL_TO_MOVE


def test_the_beads_tracker_config_is_backed_up_optional_and_config_only():
    out = subprocess.run([sys.executable, str(REPO / "tools/graph/backup_stores.py"), "stores"],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    rows = {line.split("\t")[0]: line.split("\t") for line in out.splitlines()}
    assert rows["beads"][1:4] == ["dir", "beads-config", "optional"]


def test_the_beads_config_action_copies_tracker_files_never_dolt_data(tmp_path):
    beads = tmp_path / ".beads"
    (beads / "orgs" / "anchore").mkdir(parents=True)
    (beads / "dolt" / "data").mkdir(parents=True)
    for f in ("credentials.env", "config.yaml", "metadata.json"):
        (beads / f).write_text(f)
        (beads / "orgs" / "anchore" / f).write_text("anchore-" + f)
    (beads / "dolt" / "data" / "big.db").write_text("x")
    (beads / "dolt" / ".beads-credential-key").write_text("k")
    text = (REPO / "tools/graph/backup-all.sh").read_text()
    start = text.index("        beads-config)")
    loop = text[text.index("for cfg", start):text.index("done ;;", start) + len("done")]
    script = f'''
        resolved={beads}; rel=.beads
        backup_copy() {{ echo "$1"; }}
        {loop}
    '''
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         timeout=30, check=True).stdout.split()
    assert sorted(out) == sorted([
        ".beads/credentials.env", ".beads/config.yaml", ".beads/metadata.json",
        ".beads/orgs/anchore/credentials.env", ".beads/orgs/anchore/config.yaml",
        ".beads/orgs/anchore/metadata.json"])
