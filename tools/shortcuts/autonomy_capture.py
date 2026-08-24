#!/usr/bin/env python3
"""Generate the unsigned, reusable Autonomy Capture Apple Shortcut.

The artifact contains no server URL or bearer credential.  Configuration is
supplied as Shortcut Input by the dashboard pairing URL.  The Shortcut enrolls
against that origin, waits for operator approval, and stores the resulting
upload-only credential in its private iCloud Shortcuts directory.

Run this on any platform with Python.  The resulting plist must be signed on a
Mac with::

    shortcuts sign --mode anyone --input Autonomy-Capture.shortcut \
        --output Autonomy-Capture-signed.shortcut
"""
from __future__ import annotations

import argparse
import plistlib
import uuid
from pathlib import Path
from typing import Any


NAME = "Autonomy Capture"
CONFIG_PATH = "Autonomy Capture/config.txt"
CLIENT_VERSION = "3612.0.2.1"
MINIMUM_CLIENT_VERSION = 900
PLACEHOLDER = "\ufffc"
_NAMESPACE = uuid.UUID("f4a55754-3f4b-4db9-94c2-8c7567c384d5")


def _uuid(label: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, label)).upper()


def _action(identifier: str, **parameters: Any) -> dict[str, Any]:
    return {
        "WFWorkflowActionIdentifier": identifier,
        "WFWorkflowActionParameters": parameters,
    }


def _output(label: str, name: str) -> dict[str, str]:
    return {"OutputUUID": _uuid(label), "Type": "ActionOutput", "OutputName": name}


def _variable(name: str) -> dict[str, str]:
    return {"VariableName": name, "Type": "Variable"}


def _shortcut_input() -> dict[str, str]:
    return {"Type": "ExtensionInput"}


def _attachment(reference: dict[str, Any]) -> dict[str, Any]:
    return {"Value": reference, "WFSerializationType": "WFTextTokenAttachment"}


def _token_string(*parts: str | dict[str, Any]) -> dict[str, Any]:
    text = ""
    attachments: dict[str, dict[str, Any]] = {}
    for part in parts:
        if isinstance(part, str):
            text += part
            continue
        offset = len(text)
        text += PLACEHOLDER
        attachments[f"{{{offset}, 1}}"] = part
    return {
        "Value": {"string": text, "attachmentsByRange": attachments},
        "WFSerializationType": "WFTextTokenString",
    }


def _headers(*items: tuple[str, str | dict[str, Any]]) -> dict[str, Any]:
    fields = []
    for key, value in items:
        fields.append({
            "WFItemType": 0,
            "WFKey": _token_string(key),
            "WFValue": _token_string(value) if isinstance(value, str) else value,
        })
    return {
        "Value": {"WFDictionaryFieldValueItems": fields},
        "WFSerializationType": "WFDictionaryFieldValue",
    }


def _conditional_start(group: str, reference: dict[str, Any]) -> dict[str, Any]:
    return _action(
        "is.workflow.actions.conditional",
        GroupingIdentifier=group,
        WFControlFlowMode=0,
        WFCondition=100,  # Has Any Value
        WFInput={"Type": "Variable", "Variable": _attachment(reference)},
    )


def _control_marker(identifier: str, group: str, mode: int) -> dict[str, Any]:
    return _action(identifier, GroupingIdentifier=group, WFControlFlowMode=mode)


def build_workflow() -> dict[str, Any]:
    """Return the complete unsigned workflow plist object."""
    mode_group = _uuid("mode-group")
    poll_group = _uuid("poll-group")

    remember_pairing_origin = _action(
        "is.workflow.actions.setvariable",
        WFVariableName="Autonomy Origin",
        WFInput=_attachment(_shortcut_input()),
    )
    enrollment_body = _action(
        "is.workflow.actions.gettext",
        WFTextActionText=(
            '{"label":"iPhone Action Button",'
            '"requested_ttl_seconds":31536000}'
        ),
        UUID=_uuid("enrollment-body"),
    )
    create_enrollment = _action(
        "is.workflow.actions.downloadurl",
        Advanced=True,
        ShowHeaders=False,
        WFHTTPMethod="POST",
        WFHTTPBodyType="File",
        WFHTTPHeaders=_headers(("Content-Type", "application/json")),
        WFURL=_token_string(
            _variable("Autonomy Origin"), "/api/dropbox/enrollments",
        ),
        WFRequestVariable=_attachment(_output("enrollment-body", "Text")),
        UUID=_uuid("create-enrollment"),
    )
    enrollment_id = _action(
        "is.workflow.actions.getvalueforkey",
        WFGetDictionaryValueType="Value",
        WFDictionaryKey="id",
        WFInput=_attachment(_output("create-enrollment", "Contents of URL")),
        UUID=_uuid("enrollment-id"),
    )

    poll_start = _action(
        "is.workflow.actions.repeat.count",
        GroupingIdentifier=poll_group,
        WFControlFlowMode=0,
        WFRepeatCount=3,
        UUID=_uuid("poll-start"),
    )
    poll_request = _action(
        "is.workflow.actions.downloadurl",
        Advanced=True,
        ShowHeaders=False,
        WFHTTPMethod="GET",
        WFURL=_token_string(
            _variable("Autonomy Origin"),
            "/api/dropbox/enrollments/",
            _output("enrollment-id", "Dictionary Value"),
            "?wait=60",
        ),
        UUID=_uuid("poll-request"),
    )
    remember_poll = _action(
        "is.workflow.actions.setvariable",
        WFVariableName="Enrollment Response",
        WFInput=_attachment(_output("poll-request", "Contents of URL")),
    )
    poll_end = _action(
        "is.workflow.actions.repeat.count",
        GroupingIdentifier=poll_group,
        WFControlFlowMode=2,
        UUID=_uuid("poll-end"),
    )

    upload_token = _action(
        "is.workflow.actions.getvalueforkey",
        WFGetDictionaryValueType="Value",
        WFDictionaryKey="token",
        WFInput=_attachment(_variable("Enrollment Response")),
        UUID=_uuid("upload-token"),
    )
    config_text = _action(
        "is.workflow.actions.gettext",
        WFTextActionText=_token_string(
            _variable("Autonomy Origin"),
            "\n",
            _output("upload-token", "Dictionary Value"),
        ),
        UUID=_uuid("config-text"),
    )
    save_config = _action(
        "is.workflow.actions.documentpicker.save",
        WFInput=_attachment(_output("config-text", "Text")),
        WFAskWhereToSave=False,
        WFFileDestinationPath=CONFIG_PATH,
        WFSaveFileOverwrite=True,
        UUID=_uuid("save-config"),
    )
    configured = _action(
        "is.workflow.actions.notification",
        WFNotificationActionTitle=NAME,
        WFNotificationActionBody="Configured",
        WFNotificationActionSound=False,
        UUID=_uuid("configured-notification"),
    )

    open_config = _action(
        "is.workflow.actions.documentpicker.open",
        WFFileErrorIfNotFound=True,
        WFGetFolderContents=False,
        WFGetFilePath=CONFIG_PATH,
        UUID=_uuid("open-config"),
    )
    split_config = _action(
        "is.workflow.actions.text.split",
        text=_attachment(_output("open-config", "File")),
        WFTextSeparator="New Lines",
        ShowWhenRun=False,
        UUID=_uuid("split-config"),
    )
    config_origin = _action(
        "is.workflow.actions.getitemfromlist",
        WFItemSpecifier="Item At Index",
        WFItemIndex=1,
        WFInput=_attachment(_output("split-config", "Split Text")),
        UUID=_uuid("config-origin"),
    )
    remember_origin = _action(
        "is.workflow.actions.setvariable",
        WFVariableName="Autonomy Origin",
        WFInput=_attachment(_output("config-origin", "Item from List")),
    )
    config_token = _action(
        "is.workflow.actions.getitemfromlist",
        WFItemSpecifier="Item At Index",
        WFItemIndex=2,
        WFInput=_attachment(_output("split-config", "Split Text")),
        UUID=_uuid("config-token"),
    )
    remember_token = _action(
        "is.workflow.actions.setvariable",
        WFVariableName="Upload Token",
        WFInput=_attachment(_output("config-token", "Item from List")),
    )
    screenshot = _action(
        "is.workflow.actions.takescreenshot",
        UUID=_uuid("screenshot"),
    )
    upload = _action(
        "is.workflow.actions.downloadurl",
        Advanced=True,
        ShowHeaders=False,
        WFHTTPMethod="POST",
        WFHTTPBodyType="File",
        WFHTTPHeaders=_headers(
            (
                "Authorization",
                _token_string("Bearer ", _variable("Upload Token")),
            ),
            ("Content-Type", "image/png"),
            ("X-Autonomy-Filename", "iphone-screenshot.png"),
        ),
        WFURL=_token_string(_variable("Autonomy Origin"), "/api/dropbox"),
        WFRequestVariable=_attachment(_output("screenshot", "Screenshot")),
        UUID=_uuid("upload"),
    )
    sent = _action(
        "is.workflow.actions.notification",
        WFNotificationActionTitle=NAME,
        WFNotificationActionBody="Screenshot sent",
        WFNotificationActionSound=False,
        UUID=_uuid("sent-notification"),
    )

    actions = [
        _conditional_start(mode_group, _shortcut_input()),
        remember_pairing_origin,
        enrollment_body,
        create_enrollment,
        enrollment_id,
        poll_start,
        poll_request,
        remember_poll,
        poll_end,
        upload_token,
        config_text,
        save_config,
        configured,
        _control_marker("is.workflow.actions.conditional", mode_group, 1),
        open_config,
        split_config,
        config_origin,
        remember_origin,
        config_token,
        remember_token,
        screenshot,
        upload,
        sent,
        _control_marker("is.workflow.actions.conditional", mode_group, 2),
    ]
    return {
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": CLIENT_VERSION,
        "WFWorkflowMinimumClientVersion": MINIMUM_CLIENT_VERSION,
        "WFWorkflowMinimumClientVersionString": str(MINIMUM_CLIENT_VERSION),
        "WFWorkflowIcon": {
            "WFWorkflowIconGlyphNumber": 59511,
            "WFWorkflowIconStartColor": 4282601983,
        },
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": [],
        "WFWorkflowName": NAME,
    }


def write_shortcut(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        plistlib.dump(build_workflow(), output, fmt=plistlib.FMT_BINARY, sort_keys=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("Autonomy-Capture.shortcut"),
    )
    args = parser.parse_args()
    write_shortcut(args.output)
    print(args.output.resolve())


if __name__ == "__main__":
    main()
