"""Generate an isolated, lightweight OIDC/Caddy Service gate proof.

OAuth2 Proxy owns OIDC and cookie validation; this module only renders its
deployment. The HTTP listener is a private target behind the node's existing
TLS gateway, never a standalone public HTTP endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
from dataclasses import dataclass
from urllib.parse import urlsplit


AUTH_IMAGE = "quay.io/oauth2-proxy/oauth2-proxy:v7.15.4@sha256:b1b2021fe8f4004573e8d690dec6c7bb29cc44364572cf8510a05bf3a0ae2ded"
CADDY_IMAGE = "caddy:2.11.2-alpine@sha256:834468128c7696cec0ceea6172f7d692daf645ae51983ca76e39da54a97c570d"
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")


@dataclass(frozen=True)
class GateConfig:
    name: str
    hosts: tuple[str, ...]
    issuer: str
    client_id: str
    port: int

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", self.name):
            raise ValueError("invalid instance name")
        if not self.hosts or len(self.hosts) > 32 or len(set(self.hosts)) != len(self.hosts):
            raise ValueError("one to 32 distinct hosts required")
        for host in self.hosts:
            if len(host) > 253 or not _HOST.fullmatch(host):
                raise ValueError("hosts must be exact lowercase DNS names without ports")
        validate_provider(self.issuer, self.client_id)
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("port must be 1024..65535")


def validate_provider(issuer_url: str, client_id: str) -> None:
    """Shared by the existing proof configuration and organization Settings."""
    issuer = urlsplit(issuer_url)
    if (
        issuer.scheme != "https" or not issuer.hostname
        or not _HOST.fullmatch(issuer.hostname)
        or issuer.netloc != issuer.hostname or issuer.query or issuer.fragment
        or re.search(r"[\s\\]", issuer_url)
    ):
        raise ValueError("issuer must be an HTTPS discovery issuer without credentials/query")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", client_id):
        raise ValueError("invalid client ID")


def render_auth_config(
    config: GateConfig, *, listener_port: int = 4180,
    cookie_expire: str = "15m", cookie_refresh: str = "0",
) -> str:
    """No tokens in the app, no database, no IdP call on ordinary checks."""
    settings = {
        "provider": "oidc",
        "oidc_issuer_url": config.issuer,
        "client_id": config.client_id,
        "client_secret_file": "/run/secrets/client-secret",
        "cookie_secret_file": "/run/secrets/cookie-secret",
        "http_address": f"127.0.0.1:{listener_port}",
        "reverse_proxy": True,
        "trusted_proxy_ips": ["127.0.0.1/32", "::1/128"],
        "upstreams": ["static://202"],
        "scope": "openid email profile",
        "email_domains": ["*"],  # Identity issuer + its app assignments are the gate.
        "code_challenge_method": "S256",
        "auth_request_response_mode": "form_post",
        "cookie_name": "__Host-autonomy_service",
        "cookie_secure": True,
        "cookie_httponly": True,
        "cookie_samesite": "lax",
        "cookie_expire": cookie_expire,
        "cookie_refresh": cookie_refresh,
        "cookie_csrf_expire": "5m",
        "cookie_csrf_samesite": "none",
        "cookie_csrf_per_request": True,
        "cookie_csrf_per_request_limit": 5,
        "session_store_type": "cookie",
        "session_cookie_minimal": True,
        "skip_provider_button": True,
        "skip_claims_from_profile_url": True,
        "pass_access_token": False,
        "pass_authorization_header": False,
        "set_authorization_header": False,
        "pass_basic_auth": False,
        "pass_user_headers": False,
        "set_xauthrequest": False,
        "request_logging": False,
        "auth_logging": False,
    }
    # JSON scalar/list literals used here are also valid TOML values.
    return "\n".join(f"{key} = {json.dumps(value)}" for key, value in settings.items()) + "\n"


def render_caddyfile(config: GateConfig) -> str:
    lines = ["{", "\tadmin off", "\tauto_https off", "}", ":8080 {"]
    for index, host in enumerate(config.hosts):
        lines += [
            f"\t@host{index} host {host}", f"\thandle @host{index} {{", "\t\troute {",
            "\t\t\trequest_header -Authorization",
            "\t\t\trequest_header -X-Auth-Request-*",
            "\t\t\trequest_header -Remote-*",
            "\t\t\trequest_header -X-Forwarded-*",
            "\t\t\trequest_header -X-Real-IP",
            "\t\t\trequest_header -X-Original-URL",
            "\t\t\trequest_header -X-Rewrite-URL",
            "\t\t\thandle /oauth2/* {",
            "\t\t\t\treverse_proxy 127.0.0.1:4180 {",
            '\t\t\t\t\theader_up X-Forwarded-Proto "https"',
            f'\t\t\t\t\theader_up X-Forwarded-Host "{host}"',
            "\t\t\t\t\theader_up X-Forwarded-Uri {uri}",
            "\t\t\t\t\theader_up X-Real-IP {remote_host}",
            "\t\t\t\t}", "\t\t\t}", "\t\t\thandle {",
            "\t\t\t\tforward_auth 127.0.0.1:4180 {",
            "\t\t\t\t\turi /oauth2/auth",
            '\t\t\t\t\theader_up X-Forwarded-Proto "https"',
            f'\t\t\t\t\theader_up X-Forwarded-Host "{host}"',
            "\t\t\t\t\theader_up X-Real-IP {remote_host}",
            "\t\t\t\t\t@unauthenticated status 401",
            "\t\t\t\t\thandle_response @unauthenticated {",
            # A leading slash is otherwise parsed as a PATH MATCHER by Caddy,
            # leaving the 401 handler unmatched and continuing to the app.
            f"\t\t\t\t\t\tredir * /oauth2/start?rd=https%3A%2F%2F{host}%2F 302",
            "\t\t\t\t\t}", "\t\t\t\t}",
            "\t\t\t\treverse_proxy app:8080",
            "\t\t\t}", "\t\t}", "\t}",
        ]
    lines += ['\thandle {', '\t\trespond "Unknown host" 421', '\t}', '}']
    return "\n".join(lines) + "\n"


def _bind(source: Path, target: str) -> dict:
    return {"type": "bind", "source": str(source), "target": target, "read_only": True}


def render_helper_service(runtime: Path, revision: str) -> dict:
    """The same lightweight helper, beside the production gateway."""
    return {
        "image": AUTH_IMAGE,
        "profiles": ["service-gateway"],
        "restart": "unless-stopped",
        "user": "1000:1000",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "network_mode": "service:service-gateway",
        "depends_on": {"service-gateway": {"condition": "service_started"}},
        "command": ["--config=/etc/oauth2-proxy.cfg"],
        "environment": {"GOMEMLIMIT": "96MiB", "GOMAXPROCS": "1"},
        "labels": {"autonomy.auth-config": revision},
        "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "2"}},
        "volumes": [
            _bind(runtime / "oauth2-proxy.cfg", "/etc/oauth2-proxy.cfg"),
            _bind(runtime / "client-secret", "/run/secrets/client-secret"),
            _bind(runtime / "cookie-secret", "/run/secrets/cookie-secret"),
        ],
    }


def render_compose(config: GateConfig, runtime: Path) -> dict:
    common = {
        "user": f"{os.getuid()}:{os.getgid()}",
        "read_only": True, "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "2"}},
    }
    return {"name": f"autonomy-oidc-{config.name}", "services": {
        "gateway": common | {
            "image": CADDY_IMAGE,
            # Official Caddy binary carries this file capability; Linux can
            # refuse exec if it is absent from the bounding set, even at :8080.
            "cap_add": ["NET_BIND_SERVICE"],
            "command": ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"],
            "ports": [f"{config.port}:8080"],
            "volumes": [_bind(runtime / "Caddyfile", "/etc/caddy/Caddyfile")],
            "environment": {"HOME": "/tmp", "XDG_CONFIG_HOME": "/tmp/config", "XDG_DATA_HOME": "/tmp/data"},
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=8m,mode=1777"],
        },
        "auth": common | {
            "image": AUTH_IMAGE,
            "network_mode": "service:gateway",
            "depends_on": {"gateway": {"condition": "service_started"}},
            "command": ["--config=/etc/oauth2-proxy.cfg"],
            "environment": {"GOMEMLIMIT": "96MiB", "GOMAXPROCS": "1"},
            "volumes": [
                _bind(runtime / "oauth2-proxy.cfg", "/etc/oauth2-proxy.cfg"),
                _bind(runtime / "client-secret", "/run/secrets/client-secret"),
                _bind(runtime / "cookie-secret", "/run/secrets/cookie-secret"),
            ],
        },
        "app": common | {
            "image": CADDY_IMAGE,
            "cap_add": ["NET_BIND_SERVICE"],
            "command": ["caddy", "respond", "--listen", ":8080", "--body", f"authenticated-service:{config.name}\n"],
            "environment": {"HOME": "/tmp"},
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=8m,mode=1777"],
        },
    }}


def create_runtime(config: GateConfig, directory: str | Path, secret_file: str | Path) -> Path:
    runtime = Path(directory).absolute()
    if not runtime.resolve().is_relative_to(Path("/tmp")) or runtime.is_symlink():
        raise ValueError("runtime must be on local /tmp, never repo/shared/output storage")
    if os.getuid() == 0:
        raise ValueError("run as a nonroot user; containers inherit that UID")
    source = Path(secret_file)
    metadata = source.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise ValueError("client secret must be a private owner-only regular file")
    secret = source.read_bytes()
    if not 1 <= len(secret) <= 8192 or any(c in secret for c in (b"\n", b"\r", b"\x00")):
        raise ValueError("client secret must be nonempty, bounded, without line endings")
    runtime.mkdir(mode=0o700)  # Never overwrite a previous runtime/key.
    files = {
        "client-secret": secret, "cookie-secret": secrets.token_bytes(32),
        "oauth2-proxy.cfg": render_auth_config(config).encode(),
        "Caddyfile": render_caddyfile(config).encode(),
        "compose.json": (json.dumps(render_compose(config, runtime), indent=2) + "\n").encode(),
    }
    for name, content in files.items():
        fd = os.open(runtime / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
    return runtime / "compose.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--host", action="append", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--client-secret-file", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    config = GateConfig(args.name, tuple(args.host), args.issuer, args.client_id, args.port)
    print(create_runtime(config, args.runtime_dir, args.client_secret_file))


if __name__ == "__main__":
    main()
