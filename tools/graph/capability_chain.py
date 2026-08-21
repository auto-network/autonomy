"""One capability-chain validation primitive for readiness and launch.

An enabled workspace capability is a four-row chain::

    workspace enable -> capability contract -> org install -> implementation

Two of those edges carry versions in adjacent fields and one (enable ->
install) is expressed by the shared contract key.  A plain ``references=``
walk cannot validate either shape.  This module keeps the cross-row rules
pure so the Settings readiness walker and the workspace launcher consume the
same answer instead of independently deciding whether to omit a capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class CapabilityChainIssue:
    """A broken edge in an enabled workspace capability chain."""

    kind: str
    detail: str
    field: str
    subject: str
    looked_in: str
    set_id: str = ""
    key: str = ""


@dataclass(frozen=True)
class CapabilityChain:
    """The validated rows and resolved versions needed by materialization."""

    contract: str
    contract_version: int
    install: Mapping[str, Any]
    implementation: Mapping[str, Any]


def validate_capability_chain(
    *,
    contract_key: str,
    enable: Mapping[str, Any],
    contract: Mapping[str, Any] | None,
    install: Mapping[str, Any] | None,
    implementation: Mapping[str, Any] | None,
) -> tuple[CapabilityChain | None, tuple[CapabilityChainIssue, ...]]:
    """Validate one enabled chain without reading Settings.

    Callers provide the rows they resolved under their own visibility rules.
    Disabled enable rows deliberately have neither a chain nor findings.
    """
    if enable.get("enabled", True) is False:
        return None, ()

    issues: list[CapabilityChainIssue] = []
    declared_contract = enable.get("contract")
    if declared_contract != contract_key:
        issues.append(CapabilityChainIssue(
            "capability_contract_key_mismatch",
            f"enable key names contract {contract_key!r}, but its payload names "
            f"{declared_contract!r}",
            "contract",
            str(declared_contract or contract_key),
            "the workspace capability enable row",
            set_id="autonomy.workspace.capability.enable",
        ))

    if install is None:
        issues.append(CapabilityChainIssue(
            "missing_capability_install",
            f"enabled contract {contract_key!r} has no organization installation",
            "contract",
            contract_key,
            "the workspace organization's capability installations",
            set_id="autonomy.org.capability.install",
            key=contract_key,
        ))

    install_version = install.get("contract_version") if install else None
    enable_version = enable.get("contract_version")
    resolved_version = enable_version if enable_version is not None else install_version
    if not isinstance(resolved_version, int) or isinstance(resolved_version, bool):
        issues.append(CapabilityChainIssue(
            "missing_capability_contract_version",
            f"enabled contract {contract_key!r} resolves no integer contract version",
            "contract_version",
            f"{contract_key}@?",
            "the enable row and organization installation",
            set_id="autonomy.org.capability.install",
            key=contract_key,
        ))
    else:
        subject = f"{contract_key}@{resolved_version}"
        if contract is None:
            issues.append(CapabilityChainIssue(
                "missing_capability_contract_version",
                f"enabled contract requires {subject}, but no such contract row resolves",
                "contract_version",
                subject,
                "the organization's visible capability contracts",
                set_id="autonomy.capability.contract",
                key=contract_key,
            ))
        elif (contract.get("name"), contract.get("version")) != (
            contract_key, resolved_version
        ):
            issues.append(CapabilityChainIssue(
                "capability_contract_version_mismatch",
                f"enabled contract requires {subject}, but the resolved contract row "
                f"declares {contract.get('name')!r}@{contract.get('version')!r}",
                "contract_version",
                subject,
                "the resolved capability contract row",
                set_id="autonomy.capability.contract",
                key=contract_key,
            ))
        if install is not None and install_version != resolved_version:
            issues.append(CapabilityChainIssue(
                "capability_install_contract_version_mismatch",
                f"workspace resolves {subject}, but the organization installation "
                f"pins {contract_key}@{install_version!r}",
                "contract_version",
                subject,
                "the workspace enable and organization installation rows",
                set_id="autonomy.org.capability.install",
                key=contract_key,
            ))

    if install is not None and install.get("contract") != contract_key:
        issues.append(CapabilityChainIssue(
            "capability_install_contract_mismatch",
            f"installation key names {contract_key!r}, but its payload names "
            f"{install.get('contract')!r}",
            "contract",
            contract_key,
            "the organization capability installation row",
            set_id="autonomy.org.capability.install",
            key=contract_key,
        ))

    impl_name = install.get("implementation") if install else None
    impl_version = install.get("implementation_version") if install else None
    impl_subject = f"{impl_name}@{impl_version}" if impl_name else "implementation@?"
    if install is not None and (
        not isinstance(impl_name, str)
        or not isinstance(impl_version, int)
        or isinstance(impl_version, bool)
    ):
        issues.append(CapabilityChainIssue(
            "invalid_capability_implementation_pin",
            "organization installation does not name an implementation and integer version",
            "implementation_version",
            impl_subject,
            "the organization capability installation row",
            set_id="autonomy.org.capability.install",
            key=contract_key,
        ))
    elif install is not None:
        if implementation is None:
            issues.append(CapabilityChainIssue(
                "missing_capability_implementation_version",
                f"organization installation pins {impl_subject}, but that version does "
                "not resolve",
                "implementation_version",
                impl_subject,
                "the organization's visible capability implementations",
                set_id="autonomy.org.capability.install",
                key=contract_key,
            ))
        elif (implementation.get("name"), implementation.get("version")) != (
            impl_name, impl_version
        ):
            issues.append(CapabilityChainIssue(
                "missing_capability_implementation_version",
                f"organization installation pins {impl_subject}, but the resolved "
                f"implementation row declares {implementation.get('name')!r}@"
                f"{implementation.get('version')!r}",
                "implementation_version",
                impl_subject,
                "the resolved capability implementation row",
                set_id="autonomy.org.capability.install",
                key=contract_key,
            ))

    if (
        implementation is not None
        and isinstance(resolved_version, int)
        and not isinstance(resolved_version, bool)
    ):
        declared = {
            (ref.get("contract"), ref.get("version"))
            for ref in implementation.get("implements", ())
            if isinstance(ref, Mapping)
        }
        if (contract_key, resolved_version) not in declared:
            issues.append(CapabilityChainIssue(
                "capability_implementation_contract_mismatch",
                f"implementation {impl_subject} does not declare that it implements "
                f"{contract_key}@{resolved_version}",
                "implements",
                f"{contract_key}@{resolved_version}",
                "the resolved capability implementation row",
                set_id="autonomy.capability.impl",
                key=str(impl_name or ""),
            ))

    if issues or install is None or implementation is None or not isinstance(
        resolved_version, int
    ):
        return None, tuple(issues)
    return CapabilityChain(
        contract=contract_key,
        contract_version=resolved_version,
        install=install,
        implementation=implementation,
    ), ()
