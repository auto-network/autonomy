from tools.shortcuts import autonomy_capture


def _actions():
    return autonomy_capture.build_workflow()["WFWorkflowActions"]


def test_artifact_contains_no_origin_or_bearer():
    wire = repr(autonomy_capture.build_workflow())
    assert "https://" not in wire
    assert "Bearer " in wire  # header scheme only; value is a variable
    assert "Upload Token" in wire
    assert "global_operator_dropbox" not in wire


def test_pairing_and_capture_are_two_branches():
    actions = _actions()
    conditionals = [
        action["WFWorkflowActionParameters"]
        for action in actions
        if action["WFWorkflowActionIdentifier"] == "is.workflow.actions.conditional"
    ]
    assert [item["WFControlFlowMode"] for item in conditionals] == [0, 1, 2]
    assert conditionals[0]["WFCondition"] == 100
    assert conditionals[0]["WFInput"]["Variable"]["Value"] == {
        "Type": "ExtensionInput"
    }


def test_pairing_uses_only_public_enrollment_routes():
    wire = repr(_actions())
    assert "/api/dropbox/enrollments" in wire
    assert "?wait=60" in wire
    assert "requested_ttl_seconds" in wire
    assert "global_operator_dropbox" not in wire


def test_pairing_materializes_input_as_origin_and_requests_body_not_headers():
    actions = _actions()
    first_set = next(
        action["WFWorkflowActionParameters"]
        for action in actions
        if action["WFWorkflowActionIdentifier"] == "is.workflow.actions.setvariable"
    )
    assert first_set["WFVariableName"] == "Autonomy Origin"
    assert first_set["WFInput"]["Value"] == {"Type": "ExtensionInput"}

    create = next(
        action["WFWorkflowActionParameters"]
        for action in actions
        if action["WFWorkflowActionIdentifier"] == "is.workflow.actions.downloadurl"
        and action["WFWorkflowActionParameters"].get("WFHTTPBodyType") == "File"
        and len(
            action["WFWorkflowActionParameters"]["WFHTTPHeaders"]["Value"][
                "WFDictionaryFieldValueItems"
            ]
        ) == 1
    )
    assert create["ShowHeaders"] is False
    assert create["WFURL"]["WFSerializationType"] == "WFTextTokenString"

    # Apple's ToolKit schema declares WFURL as a string. A generic token
    # attachment imports cleanly but is rejected as an invalid URL at runtime.
    downloads = [
        action["WFWorkflowActionParameters"]
        for action in actions
        if action["WFWorkflowActionIdentifier"] == "is.workflow.actions.downloadurl"
    ]
    assert not any(
        action["WFWorkflowActionIdentifier"] == "is.workflow.actions.url"
        for action in actions
    )
    assert all(
        parameters["WFURL"]["WFSerializationType"] == "WFTextTokenString"
        for parameters in downloads
    )


def test_capture_posts_file_with_upload_headers():
    downloads = [
        action["WFWorkflowActionParameters"]
        for action in _actions()
        if action["WFWorkflowActionIdentifier"] == "is.workflow.actions.downloadurl"
    ]
    upload = downloads[-1]
    assert upload["WFHTTPMethod"] == "POST"
    assert upload["WFHTTPBodyType"] == "File"
    header_items = upload["WFHTTPHeaders"]["Value"]["WFDictionaryFieldValueItems"]
    keys = [item["WFKey"]["Value"]["string"] for item in header_items]
    assert keys == ["Authorization", "Content-Type", "X-Autonomy-Filename"]
    auth = header_items[0]["WFValue"]["Value"]
    assert auth["string"] == "Bearer \ufffc"
    assert list(auth["attachmentsByRange"].values()) == [
        {"VariableName": "Upload Token", "Type": "Variable"}
    ]


def test_config_is_fixed_path_and_overwritten():
    actions = _actions()
    save = next(
        action["WFWorkflowActionParameters"]
        for action in actions
        if action["WFWorkflowActionIdentifier"]
        == "is.workflow.actions.documentpicker.save"
    )
    load = next(
        action["WFWorkflowActionParameters"]
        for action in actions
        if action["WFWorkflowActionIdentifier"]
        == "is.workflow.actions.documentpicker.open"
    )
    assert save["WFFileDestinationPath"] == autonomy_capture.CONFIG_PATH
    assert save["WFSaveFileOverwrite"] is True
    assert save["WFAskWhereToSave"] is False
    assert load["WFGetFilePath"] == autonomy_capture.CONFIG_PATH
    assert load["WFFileErrorIfNotFound"] is True
