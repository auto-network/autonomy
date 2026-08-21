"""Pure parsing for launcher environment source declarations.

Parsing answers only what a declaration names. It performs no filesystem,
environment, or vault read, so readiness metadata and launch materialization
can share the grammar without moving secret values across their boundary.
"""

from __future__ import annotations

from dataclasses import dataclass


HOST_ENV_PREFIX = "host:"
FILE_ENV_PREFIX = "file:"
CREDENTIAL_ENV_PREFIX = "credential:"


@dataclass(frozen=True)
class EnvSource:
    kind: str
    locator: str = ""
    variable: str = ""
    literal: str = ""
    valid: bool = True
    error: str = ""


def parse_capability_env_source(source: str) -> EnvSource:
    """Parse the full capability ``env_bindings`` source grammar."""
    if source.startswith(HOST_ENV_PREFIX):
        variable = source[len(HOST_ENV_PREFIX):].strip()
        return EnvSource(
            "host", variable=variable, valid=bool(variable),
            error="empty_variable" if not variable else "",
        )

    if source.startswith(FILE_ENV_PREFIX):
        rest = source[len(FILE_ENV_PREFIX):]
        if ":" not in rest:
            return EnvSource("file", valid=False, error="missing_separator")
        path, variable = rest.rsplit(":", 1)
        variable = variable.strip()
        return EnvSource(
            "file", locator=path, variable=variable,
            valid=bool(path and variable),
            error="empty_path_or_variable" if not (path and variable) else "",
        )

    if source.startswith(CREDENTIAL_ENV_PREFIX):
        key = source[len(CREDENTIAL_ENV_PREFIX):].strip()
        return EnvSource(
            "credential", locator=key, valid=bool(key),
            error="empty_key" if not key else "",
        )

    return EnvSource("literal", literal=source)


def parse_workspace_env_source(source: str) -> EnvSource:
    """Parse workspace ``env`` values, where only ``credential:`` is special."""
    if source.startswith(CREDENTIAL_ENV_PREFIX):
        key = source[len(CREDENTIAL_ENV_PREFIX):].strip()
        return EnvSource(
            "credential", locator=key, valid=bool(key),
            error="empty_key" if not key else "",
        )
    return EnvSource("literal", literal=source)
