"""``autonomy.browser.defaults#1`` — browser broker limits (graph://c330323d-986).

Every default under the design's "Decisions": lease time limits and idle
release, the per-lease memory, CPU and process caps, node admission (lease
count and free disk), download limits and retention, and the vault set new
passkeys go to. Machine-homed: how many browsers this computer can run is a
fact about this computer. Read by the dashboard on every lease request and
reconciler cycle; schema defaults apply when no row has been written.
"""

from __future__ import annotations

from .registry import (
    SchemaValidationError,
    SettingSchema,
    home,
    publication_band,
    singleton,
)

SET_ID = "autonomy.browser.defaults"
SCHEMA_REVISION = 1

#: name -> (default, minimum, maximum, description)
LIMITS: dict[str, tuple[int, int, int, str]] = {
    "ephemeral_ttl_s": (1800, 60, 86_400, "Time limit of an ephemeral lease, in seconds."),
    "persistent_ttl_s": (7200, 60, 86_400, "Time limit of a persistent lease, in seconds."),
    "idle_s": (600, 60, 86_400, "A lease is released after this many seconds without activity."),
    "memory_mb": (2048, 512, 16_384, "Memory cap per lease container, MiB; no swap."),
    "cpus": (2, 1, 16, "CPU cap per lease container."),
    "pids": (1024, 128, 8192, "Process cap per lease container."),
    "max_leases": (4, 0, 16, "Leases that may run on this node at once; 0 admits none."),
    "min_free_gib": (20, 1, 10_000, "A lease is admitted only with this much free on the data volume."),
    "download_max_mb": (100, 1, 4096, "Largest single downloaded file, MB."),
    "org_download_total_mb": (2048, 1, 1_048_576, "Downloaded files one organization may hold, MB."),
    "download_keep_after_ack_h": (24, 1, 720, "Hours a file is kept after the caller acknowledges it."),
    "download_max_age_d": (7, 1, 90, "Days after which a downloaded file is deleted regardless."),
}
PASSKEY_VAULT_SETS = ("audited", "secured")
DEFAULT_PASSKEY_VAULT_SET = "audited"


def resolved(payload: dict | None) -> dict:
    """The payload with every field present: stored values, else defaults."""
    out = {name: spec[0] for name, spec in LIMITS.items()}
    out["passkey_vault_set"] = DEFAULT_PASSKEY_VAULT_SET
    for name, value in (payload or {}).items():
        if name in LIMITS and isinstance(value, int) and not isinstance(value, bool):
            out[name] = value
        elif name == "passkey_vault_set" and value in PASSKEY_VAULT_SETS:
            out[name] = value
    return out


SYNOPSIS = {
    "summary": "Browser broker limits: lease time limits, per-lease caps, node admission, downloads",
    "nouns": ["browser lease", "browser broker", "lease limit", "memory cap", "admission"],
    "related_set_ids": [],
}


@publication_band(min="raw", max="curated")
@home("machine")
@singleton(key="default")
class BrowserDefaultsV1(SettingSchema):
    """Shape of an ``autonomy.browser.defaults#1`` payload."""

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        **{name: {"type": "integer", "description": spec[3], "default": spec[0]}
           for name, spec in LIMITS.items()},
        "passkey_vault_set": {
            "type": "string",
            "description": "Vault set a newly registered passkey goes to: audited or secured.",
            "default": DEFAULT_PASSKEY_VAULT_SET,
        },
    }

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)
        for name, (_, low, high, _) in LIMITS.items():
            value = payload.get(name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise SchemaValidationError(
                    f"{cls.__name__}: {name!r} must be an integer in {low}..{high}, got {value!r}")
        vault_set = payload.get("passkey_vault_set")
        if vault_set is not None and vault_set not in PASSKEY_VAULT_SETS:
            raise SchemaValidationError(
                f"{cls.__name__}: passkey_vault_set must be one of {PASSKEY_VAULT_SETS}")
