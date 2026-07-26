"""Host-side SMTP delivery for one secret organization invitation link.

The caller has already issued the ledger invitation and minted its
``org:join`` grant. This module sends that single link once. SMTP credentials
stay in the dashboard process: configuration stores only a host-file path,
and neither results nor errors include the password or fragment-bearing link.
"""

from __future__ import annotations

import argparse
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit


class InviteEmailError(Exception):
    """Invitation email configuration or delivery failed safely."""


MAX_EMAIL_EXPIRY_MS = 253_402_300_799_999


def _single_address(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\r" in value
        or "\n" in value
    ):
        return False
    addresses = getaddresses([value])
    return len(addresses) == 1 and bool(addresses[0][1])


def _installed_config(org: str | None) -> dict:
    try:
        from tools.graph import ops as graph_ops

        members = graph_ops.read_set(
            "autonomy.org.capability.install",
            org=org,
            peers=[],
        )
        for member in getattr(members, "members", []) or []:
            payload = member.payload if isinstance(member.payload, dict) else {}
            if payload.get("contract") == "email_sender":
                config = payload.get("broker_config") or {}
                return config if isinstance(config, dict) else {}
    except Exception:
        pass
    return {}


def _parse_bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise InviteEmailError(
        f"email sender is not configured: {name} must be a boolean"
    )


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    from_addr: str
    username: str | None
    password: str | None
    starttls: bool

    @classmethod
    def resolve(cls, org: str | None = None) -> "SmtpConfig":
        """Resolve the org install with per-value host environment overlays.

        ``host``, ``port``, and ``from_addr`` are required. Authentication is
        an optional pair: username and host-file password must both be present
        or both absent. STARTTLS is independently configurable.
        """
        installed = _installed_config(org)
        host = (
            os.environ.get("AUTONOMY_SMTP_HOST")
            or str(installed.get("host") or "")
        ).strip()
        raw_port = (
            os.environ.get("AUTONOMY_SMTP_PORT")
            or installed.get("port")
            or "587"
        )
        from_addr = (
            os.environ.get("AUTONOMY_SMTP_FROM")
            or str(installed.get("from_addr") or "")
        ).strip()
        username = (
            os.environ.get("AUTONOMY_SMTP_USERNAME")
            or str(installed.get("username") or "")
        ).strip()
        raw_starttls = os.environ.get("AUTONOMY_SMTP_STARTTLS")
        if raw_starttls is None:
            raw_starttls = installed.get("starttls", True)
        starttls = _parse_bool(raw_starttls, "starttls")

        password_path_value = (
            os.environ.get("AUTONOMY_SMTP_PASSWORD_FILE")
            or installed.get("password_file")
            or ""
        )
        password = ""
        if password_path_value:
            try:
                password = (
                    Path(str(password_path_value))
                    .expanduser()
                    .read_text(encoding="utf-8")
                    .strip()
                )
            except OSError:
                password = ""

        missing: list[str] = []
        if not host:
            missing.append("host")
        try:
            port = int(raw_port)
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            port = 0
            missing.append("port")
        if not from_addr:
            missing.append("from-address")
        elif not _single_address(from_addr):
            raise InviteEmailError(
                "email sender is not configured: from-address must be "
                "a single email address"
            )
        if username and not password:
            missing.append("password")
        if password and not username:
            missing.append("username")
        if missing:
            raise InviteEmailError(
                "email sender is not configured: missing "
                f"{', '.join(missing)} (set broker_config on the "
                "email_sender org install Setting or AUTONOMY_SMTP_*; "
                f"org={org!r})"
            )
        return cls(
            host=host,
            port=port,
            from_addr=from_addr,
            username=username or None,
            password=password or None,
            starttls=starttls,
        )


def _smtp_client(cfg: SmtpConfig) -> smtplib.SMTP:
    return smtplib.SMTP(cfg.host, cfg.port)


def _close_client(client) -> None:
    try:
        client.quit()
    except Exception:
        try:
            client.close()
        except Exception:
            pass


def validate_delivery(to_addr: object, join_link: object, expiry: object) -> None:
    """Validate all caller-controlled message fields before SMTP is opened."""
    if not _single_address(to_addr):
        raise InviteEmailError("invitation recipient must be a single address")
    if not isinstance(join_link, str) or not join_link.strip():
        raise InviteEmailError("invitation join link must be non-empty")
    try:
        parsed = urlsplit(join_link)
        fragment = parse_qs(parsed.fragment, strict_parsing=True)
    except ValueError:
        parsed = None
        fragment = {}
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or set(fragment) != {"t"}
        or len(fragment["t"]) != 1
        or not fragment["t"][0]
    ):
        raise InviteEmailError(
            "invitation join link must be an HTTP(S) URL with one #t= secret"
        )
    if (
        type(expiry) is not int
        or expiry < 0
        or expiry > MAX_EMAIL_EXPIRY_MS
    ):
        raise InviteEmailError(
            "invitation expiry must be a renderable unix-ms integer"
        )


def send_invite_email(
    to_addr: str,
    join_link: str,
    expiry: int,
    org: str | None = None,
    *,
    config_resolver: Callable[[str | None], SmtpConfig] | None = None,
) -> dict[str, str]:
    """Send exactly one invitation email and return non-secret receipt data."""
    validate_delivery(to_addr, join_link, expiry)

    resolve = config_resolver or SmtpConfig.resolve
    cfg = resolve(org)
    message_id = make_msgid(domain="auto.network")
    expires = datetime.fromtimestamp(
        expiry / 1000,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M:%S UTC")
    message = EmailMessage()
    message["To"] = to_addr.strip()
    message["From"] = cfg.from_addr
    message["Subject"] = (
        f"Invitation to join {org}" if org else "Invitation to join an organization"
    )
    message["Message-ID"] = message_id
    message.set_content(
        "You have been invited to join an organization on auto.network.\n\n"
        f"{join_link}\n\n"
        f"This invitation expires {expires}.\n\n"
        "Keep this link until you are admitted. If approval is pending, use "
        "the same link to return and complete admission.\n"
    )

    client = None
    try:
        client = _smtp_client(cfg)
        if cfg.starttls:
            client.starttls()
        if cfg.username is not None:
            client.login(cfg.username, cfg.password)
        client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        # SMTP implementations may echo command material in exception text.
        # Keep the route response independent of credentials and message body.
        raise InviteEmailError("SMTP delivery failed") from exc
    finally:
        if client is not None:
            _close_client(client)
    return {"recipient": to_addr.strip(), "message_id": message_id}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one organization invitation")
    parser.add_argument("--to", required=True)
    parser.add_argument("--join-link", required=True)
    parser.add_argument("--expiry", required=True, type=int)
    parser.add_argument("--org")
    args = parser.parse_args(argv)
    receipt = send_invite_email(
        args.to,
        args.join_link,
        args.expiry,
        org=args.org,
    )
    print(
        f"invitation sent to {receipt['recipient']} "
        f"(message-id {receipt['message_id']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
