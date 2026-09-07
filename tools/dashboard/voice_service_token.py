"""Mint the gateway credential once per dashboard supervisor startup."""
import hashlib
import os
import secrets
import tempfile
import time
from pathlib import Path
from tools.dashboard.dao import auth_db
from tools.dashboard.voice_commit_routes import PATH
from tools.data_paths import DATA_ROOT


def provision():
    destination = Path(os.environ.get("VOICE_SERVICE_TOKEN_FILE", str(DATA_ROOT / "voice-service.token")))
    token = secrets.token_urlsafe(48)
    auth_db.insert_scoped_service_token(
        hashlib.sha256(token.encode()).hexdigest(), "voice-gateway",
        capabilities=[{"method": "POST", "path": PATH}],
        application_scope="voice-sidecar", resource_audience="dashboard-local",
        source_approval_id="voice-compose-supervisor", expires_at=time.time() + 7 * 86400,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".voice-token-")
    try:
        with os.fdopen(fd, "w") as output:
            output.write(token)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    provision()
