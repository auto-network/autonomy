"""Cross-row capability integrity shared by readiness and launch."""

from tools.graph.capability_chain import validate_capability_chain


def _rows(*, contract_version=1, impl_version=5, implements_version=1):
    enable = {"contract": "test_execution", "contract_version": contract_version}
    contract = {"name": "test_execution", "version": 1}
    install = {
        "contract": "test_execution",
        "contract_version": 1,
        "implementation": "autonomy/agent-test",
        "implementation_version": impl_version,
    }
    implementation = {
        "name": "autonomy/agent-test",
        "version": 5,
        "implements": [{
            "contract": "test_execution",
            "version": implements_version,
        }],
    }
    return enable, contract, install, implementation


def _validate(*, contract_version=1, impl_version=5, implements_version=1):
    enable, contract, install, implementation = _rows(
        contract_version=contract_version,
        impl_version=impl_version,
        implements_version=implements_version,
    )
    return validate_capability_chain(
        contract_key="test_execution",
        enable=enable,
        contract=contract,
        install=install,
        implementation=implementation,
    )


def test_valid_versioned_chain_resolves():
    chain, issues = _validate()
    assert issues == ()
    assert chain is not None
    assert chain.contract_version == 1


def test_absent_pinned_implementation_version_names_the_broken_edge():
    enable, contract, install, implementation = _rows(impl_version=3)
    chain, issues = validate_capability_chain(
        contract_key="test_execution",
        enable=enable,
        contract=contract,
        install=install,
        implementation=implementation,
    )
    assert chain is None
    assert [issue.kind for issue in issues] == [
        "missing_capability_implementation_version",
    ]
    assert issues[0].subject == "autonomy/agent-test@3"


def test_contract_version_mismatch_is_explicit():
    chain, issues = _validate(contract_version=2)
    assert chain is None
    assert {issue.kind for issue in issues} == {
        "capability_contract_version_mismatch",
        "capability_install_contract_version_mismatch",
        "capability_implementation_contract_mismatch",
    }
    assert all(issue.subject == "test_execution@2" for issue in issues)


def test_implementation_must_declare_resolved_contract_version():
    chain, issues = _validate(implements_version=2)
    assert chain is None
    assert [issue.kind for issue in issues] == [
        "capability_implementation_contract_mismatch",
    ]
    assert issues[0].subject == "test_execution@1"
