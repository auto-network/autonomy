"""Trusted, non-executing remediation contract registry.

Settings may name these public contracts, but they cannot register behavior or
select callables. The planner and later execution service remain responsible
for mapping code-owned provider IDs to trusted implementations.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .schemas.registry import normalize_remediation_ref


VALID_ACTION_TYPES = (
    "setting", "vault_credential", "host_environment", "filesystem",
    "repository_auth", "manual",
)
VALID_AUTHORITIES = ("session", "operator", "operator_vault", "manual")


@dataclass(frozen=True)
class RemediationSpec:
    """Public description of a code-owned remediation family."""

    id: str
    supported_finding_kinds: tuple[str, ...]
    parameter_types: Mapping[str, str]
    action_type: str
    label: str
    description: str
    input_schema: Mapping[str, Any]
    discovery_provider_id: str | None
    preview_builder_id: str | None
    executor_id: str | None
    required_authority: str
    verification: str

    def __post_init__(self) -> None:
        # Reuse the schema-reference grammar without imposing semantic lookup.
        normalize_remediation_ref({"id": self.id, "params": {}})
        if not self.supported_finding_kinds or not all(
            isinstance(kind, str) and kind for kind in self.supported_finding_kinds
        ):
            raise ValueError("remediation spec must name supported finding kinds")
        if self.action_type not in VALID_ACTION_TYPES:
            raise ValueError(f"unknown remediation action type {self.action_type!r}")
        if self.required_authority not in VALID_AUTHORITIES:
            raise ValueError(
                f"unknown remediation authority {self.required_authority!r}"
            )
        if not self.label.strip() or not self.description.strip():
            raise ValueError("remediation spec must provide label and description")
        if not isinstance(self.parameter_types, Mapping):
            raise ValueError("remediation parameter_types must be a mapping")
        unknown_types = sorted(
            set(self.parameter_types.values())
            - {"string", "integer", "number", "boolean", "null"}
        )
        if unknown_types:
            raise ValueError(
                f"remediation spec has unknown parameter type(s) {unknown_types}"
            )
        if not isinstance(self.input_schema, Mapping):
            raise ValueError("remediation input_schema must be a mapping")
        try:
            json.dumps(self.input_schema, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("remediation input_schema must be canonical JSON data") from exc
        for provider_id in (
            self.discovery_provider_id, self.preview_builder_id, self.executor_id,
        ):
            if provider_id is not None and not (
                isinstance(provider_id, str) and provider_id
            ):
                raise ValueError("remediation provider IDs must be non-empty strings")
        object.__setattr__(self, "supported_finding_kinds", tuple(
            self.supported_finding_kinds
        ))
        object.__setattr__(self, "parameter_types", dict(self.parameter_types))
        object.__setattr__(self, "input_schema", dict(self.input_schema))

    def public_dict(self) -> dict[str, Any]:
        """Canonical JSON-safe public contract; never includes a callable."""
        return asdict(self)


_REGISTRY: dict[str, RemediationSpec] = {}


def register_remediation(spec: RemediationSpec) -> RemediationSpec:
    if spec.id in _REGISTRY:
        raise ValueError(f"duplicate remediation id {spec.id!r}")
    _REGISTRY[spec.id] = spec
    return spec


def get_remediation(remediation_id: str) -> RemediationSpec | None:
    return _REGISTRY.get(remediation_id)


def list_remediations() -> tuple[RemediationSpec, ...]:
    return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))


def remediation_registry_digest() -> str:
    public = [spec.public_dict() for spec in list_remediations()]
    canonical = json.dumps(
        public, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def validate_registered_ref(value: Any) -> tuple[str, ...]:
    """Return semantic errors without turning production import into a gate."""
    ref = normalize_remediation_ref(value)
    spec = get_remediation(ref["id"])
    if spec is None:
        return (f"unknown remediation id {ref['id']!r}",)
    unknown = sorted(set(ref["params"]) - set(spec.parameter_types))
    if unknown:
        return (f"unknown remediation parameter(s) {unknown} for {spec.id}",)
    errors: list[str] = []
    expected_types = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    for key, value in ref["params"].items():
        expected_name = spec.parameter_types[key]
        expected = expected_types.get(expected_name)
        if expected is None:
            errors.append(f"registry has unknown parameter type for {spec.id}:{key}")
            continue
        if not isinstance(value, expected) or (
            expected_name in ("integer", "number") and isinstance(value, bool)
        ):
            errors.append(
                f"remediation parameter {key!r} has the wrong type for {spec.id}"
            )
    return tuple(errors)


_NO_INPUT = {"type": "object", "properties": {}, "additionalProperties": False}


def _spec(
    remediation_id: str,
    kinds: tuple[str, ...],
    action_type: str,
    label: str,
    description: str,
    authority: str,
) -> None:
    register_remediation(RemediationSpec(
        id=remediation_id,
        supported_finding_kinds=kinds,
        parameter_types={},
        action_type=action_type,
        label=label,
        description=description,
        input_schema=_NO_INPUT,
        discovery_provider_id=None,
        preview_builder_id=None,
        executor_id=None,
        required_authority=authority,
        verification="rerun_source_readiness_predicate",
    ))


_spec(
    "workspace.env-from-host.legacy.v1", ("missing_env",), "manual",
    "Provide host environment variable",
    "Make the exact declared variable available to the launcher or fixed workspace environment.",
    "manual",
)
_spec(
    "workspace.env.credential.v1", ("missing_vault_credential", "unreadable_vault"),
    "vault_credential", "Provision workspace credential",
    "Provision the declared credential through the browser-only vault ceremony.",
    "operator_vault",
)
_spec(
    "capability.env-binding.v1", ("missing_env", "missing_path", "missing_vault_credential"),
    "manual", "Satisfy capability environment binding",
    "Satisfy the exact host, file, credential, or literal source declared by "
    "the capability installation.",
    "operator",
)
_spec(
    "workspace.declared-path.v1", (
        "missing_path", "unpopulated_path", "invalid_mount", "unanswerable_here",
    ),
    "filesystem", "Provision declared path",
    "Provision or inspect the exact path in the filesystem frame declared by the schema.",
    "operator",
)
_spec(
    "capability.install-chain.v1", (
        "capability_contract_key_mismatch",
        "missing_capability_install",
        "missing_capability_contract_version",
        "capability_contract_version_mismatch",
        "capability_install_contract_version_mismatch",
        "capability_install_contract_mismatch",
        "invalid_capability_implementation_pin",
        "missing_capability_implementation_version",
        "capability_implementation_contract_mismatch",
        "capability_implementation_contract_version_mismatch",
    ), "setting", "Repair capability installation chain",
    "Choose visible contract and implementation versions that form a complete installation chain.",
    "operator",
)
_spec(
    "repository.host-auth.v1", ("repository_auth_unavailable",),
    "repository_auth", "Configure repository host authentication",
    "Make the repository host and user authentication usable by repository preparation.",
    "manual",
)


__all__ = [
    "RemediationSpec", "get_remediation", "list_remediations",
    "register_remediation", "remediation_registry_digest",
    "validate_registered_ref",
]
