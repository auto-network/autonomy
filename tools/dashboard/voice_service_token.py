"""Mint the gateway credential once per dashboard supervisor startup.

It lives in the ramfs key cache, never the data volume (auto-es7ja): it is
re-minted at every start, so nothing needs it to persist. voice-gateway sees
only that subdirectory, read-only (docker-compose.yml)."""
import hashlib
import secrets
import time
from tools.dashboard import host_release
from tools.dashboard.dao import auth_db
from tools.dashboard.voice_commit_routes import PATH
from tools.data_paths import DATA_ROOT

RELEASE_SUBDIR = "voice"
TOKEN_FILE = "token"
#: Where the token lived before auto-es7ja: removed at provision.
LEGACY_TOKEN_FILE = DATA_ROOT / "voice-service.token"


def provision():
    token = secrets.token_urlsafe(48)
    auth_db.insert_scoped_service_token(
        hashlib.sha256(token.encode()).hexdigest(), "voice-gateway",
        capabilities=[{"method": "POST", "path": PATH}],
        application_scope="voice-sidecar", resource_audience="dashboard-local",
        source_approval_id="voice-compose-supervisor", expires_at=time.time() + 7 * 86400,
    )
    try:
        with host_release.RELEASE_LOCK:
            host_release.write_files(host_release.release_dir(RELEASE_SUBDIR),
                                     {TOKEN_FILE: token.encode()})
    finally:
        try:
            LEGACY_TOKEN_FILE.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    provision()
