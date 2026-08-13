#!/usr/bin/env python3
"""Render coturn's secret-bearing configuration into a runtime directory."""

from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import tempfile


SECRET_RE = re.compile(r"[0-9a-f]{64}")
PLACEHOLDER_RE = re.compile(r"@@[A-Z0-9_]+@@")
CADDY_PUBLIC_IP = ipaddress.ip_address("5.161.219.195")


def positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def load_secrets(path: Path) -> list[str]:
    values = [line.strip() for line in path.read_text(encoding="ascii").splitlines()]
    values = [value for value in values if value]
    if not 1 <= len(values) <= 2 or any(SECRET_RE.fullmatch(value) is None for value in values):
        raise ValueError("TURN secret file must contain one or two 64-character lowercase hex values")
    if len(set(values)) != len(values):
        raise ValueError("TURN secret file contains a duplicate value")
    return values


def write_atomic(path: Path, data: bytes, mode: int, gid: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        if gid is not None:
            os.chown(temporary, 0, gid)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def render(args: argparse.Namespace) -> None:
    public_ip = ipaddress.ip_address(args.public_ip)
    if public_ip.version != 4 or not public_ip.is_global:
        raise ValueError("TURN_PUBLIC_IP must be a globally routable IPv4 address")
    if public_ip == CADDY_PUBLIC_IP:
        raise ValueError("TURN_PUBLIC_IP must not reuse Caddy's public IPv4 address")

    minimum = positive_int(args.min_port, "TURN_RELAY_MIN_PORT")
    maximum = positive_int(args.max_port, "TURN_RELAY_MAX_PORT")
    if not 1024 <= minimum <= maximum <= 65535:
        raise ValueError("relay port range must be ordered and inside 1024..65535")
    port_count = maximum - minimum + 1
    if port_count > 1000:
        raise ValueError("relay port range exceeds the 1000-port deployment safety bound")

    user_quota = positive_int(args.user_quota, "TURN_USER_QUOTA")
    total_quota = positive_int(args.total_quota, "TURN_TOTAL_QUOTA")
    max_bps = positive_int(args.max_bps, "TURN_MAX_BPS")
    bps_capacity = positive_int(args.bps_capacity, "TURN_BPS_CAPACITY")
    if user_quota > total_quota:
        raise ValueError("per-credential quota exceeds total quota")
    if total_quota > port_count:
        raise ValueError("total quota exceeds the bounded relay port count")
    if max_bps > bps_capacity:
        raise ValueError("per-session bandwidth exceeds total bandwidth capacity")

    secrets = load_secrets(args.secrets)
    if not args.cert.is_file() or not args.key.is_file():
        raise ValueError("TLS certificate and key must both exist")
    if not args.cert.stat().st_size or not args.key.stat().st_size:
        raise ValueError("TLS certificate and key must both be non-empty")

    replacements = {
        "@@STATIC_AUTH_SECRETS@@": "\n".join(
            f"static-auth-secret={secret}" for secret in secrets
        ),
        "@@TURN_PUBLIC_IP@@": str(public_ip),
        "@@TURN_RELAY_MIN_PORT@@": str(minimum),
        "@@TURN_RELAY_MAX_PORT@@": str(maximum),
        "@@TURN_USER_QUOTA@@": str(user_quota),
        "@@TURN_TOTAL_QUOTA@@": str(total_quota),
        "@@TURN_MAX_BPS@@": str(max_bps),
        "@@TURN_BPS_CAPACITY@@": str(bps_capacity),
    }
    config = args.template.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        config = config.replace(marker, value)
    leftovers = PLACEHOLDER_RE.findall(config)
    if leftovers:
        raise ValueError(f"unresolved configuration marker: {leftovers[0]}")

    write_atomic(args.output / "turnserver.conf", config.encode(), 0o440, args.runtime_gid)
    write_atomic(args.output / "tls-cert.pem", args.cert.read_bytes(), 0o444, args.runtime_gid)
    write_atomic(args.output / "tls-key.pem", args.key.read_bytes(), 0o440, args.runtime_gid)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--template", type=Path, required=True)
    result.add_argument("--secrets", type=Path, required=True)
    result.add_argument("--cert", type=Path, required=True)
    result.add_argument("--key", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--public-ip", required=True)
    result.add_argument("--min-port", required=True)
    result.add_argument("--max-port", required=True)
    result.add_argument("--user-quota", required=True)
    result.add_argument("--total-quota", required=True)
    result.add_argument("--max-bps", required=True)
    result.add_argument("--bps-capacity", required=True)
    result.add_argument("--runtime-gid", type=int)
    return result


if __name__ == "__main__":
    render(parser().parse_args())
