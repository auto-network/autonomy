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
    assert issues[0].remediation_id == "capability.install-chain.v1"
    assert issues[0].remediation_params == {}


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


def test_every_issue_of_one_broken_chain_is_one_thing_with_the_contract_summary():
    """``browser`` and ``browser@1`` are two subjects and one repair: readiness
    groups on ``thing``, and the card says what the capability is."""
    enable, contract, _install, _implementation = _rows()
    contract = {**contract, "version": 2, "summary": "Runs tests for a session."}
    chain, issues = validate_capability_chain(
        contract_key="test_execution",
        enable=enable,
        contract=contract,
        install=None,
        implementation=None,
    )
    assert chain is None
    assert len({issue.subject for issue in issues}) > 1
    assert {issue.thing for issue in issues} == {"capability:test_execution"}
    assert {issue.name for issue in issues} == {"test_execution"}
    assert {issue.description for issue in issues} == {"Runs tests for a session."}


def test_an_unresolved_contract_still_names_the_capability():
    enable, _contract, _install, _implementation = _rows()
    _chain, issues = validate_capability_chain(
        contract_key="test_execution",
        enable=enable,
        contract=None,
        install=None,
        implementation=None,
    )
    assert issues
    assert all(issue.name == "test_execution" and issue.description == ""
               for issue in issues)
