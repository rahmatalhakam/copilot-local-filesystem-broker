from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.config import AppConfig, Workspace
from app.dispatcher import (
    OperationValidationError,
    dispatch,
)
from app.errors import PolicyViolation
from app.models import FileOperationRequest, Status


@pytest.mark.parametrize(
    ("operation", "missing_field"),
    [
        ("CREATE_FILE", "path"),
        ("CREATE_DIRECTORY", "path"),
        ("READ_FILE", "path"),
        ("LIST_DIRECTORY", "path"),
        ("UPDATE_FILE", "path"),
        ("APPEND_FILE", "path"),
        ("REPLACE_TEXT", "path"),
        ("DELETE_FILE", "path"),
        ("DELETE_DIRECTORY", "path"),
        ("MOVE", "path"),
        ("COPY", "path"),
        ("SEARCH_FILES", "path"),
        ("SEARCH_CONTENT", "path"),
        ("GET_METADATA", "path"),
        ("EXISTS", "path"),
        ("EXECUTE_COMMAND", "shellCommand"),
    ],
)
def test_dispatch_requires_operation_specific_fields(
    app_config: AppConfig,
    operation: str,
    missing_field: str,
) -> None:
    request = FileOperationRequest(
        operation=operation,
        workspace="test",
    )

    with pytest.raises(
        OperationValidationError,
        match=missing_field,
    ):
        dispatch(app_config, request)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "operation": "READ_FILE",
            "workspace": "test",
            "path": "file.txt",
            "shellCommand": "Get-Item",
        },
        {
            "operation": "EXECUTE_COMMAND",
            "workspace": "test",
            "shellCommand": "Get-Item",
            "content": "not command input",
        },
        {
            "operation": "EXISTS",
            "workspace": "test",
            "path": "file.txt",
            "force": True,
        },
    ],
)
def test_dispatch_rejects_dangerous_irrelevant_fields(
    app_config: AppConfig,
    payload: dict[str, object],
) -> None:
    request = FileOperationRequest.model_validate(payload)

    with pytest.raises(OperationValidationError):
        dispatch(app_config, request)


def test_dispatch_enforces_workspace_permission(
    app_config: AppConfig,
    workspace: Workspace,
) -> None:
    denied_workspace = replace(
        workspace,
        permissions={**workspace.permissions, "read": False},
    )
    denied_config = replace(
        app_config,
        workspaces={"test": denied_workspace},
    )
    request = FileOperationRequest(
        operation="EXISTS",
        workspace="test",
        path="example.txt",
    )

    with pytest.raises(PolicyViolation) as raised:
        dispatch(denied_config, request)

    assert raised.value.code == "OPERATION_DENIED"


def test_dispatch_create_list_search_move_copy_and_recycle_flow(
    app_config: AppConfig,
    workspace: Workspace,
) -> None:
    created_directory = dispatch(
        app_config,
        FileOperationRequest(
            operation="CREATE_DIRECTORY",
            workspace="test",
            path="notes",
        ),
    )
    created_file = dispatch(
        app_config,
        FileOperationRequest(
            operation="CREATE_FILE",
            workspace="test",
            path=r"notes\alpha.txt",
            content="Alpha needle",
            returnHash=True,
        ),
    )
    listed = dispatch(
        app_config,
        FileOperationRequest(
            operation="LIST_DIRECTORY",
            workspace="test",
            path="notes",
            includeFiles=True,
            includeDirectories=True,
        ),
    )
    name_search = dispatch(
        app_config,
        FileOperationRequest(
            operation="SEARCH_FILES",
            workspace="test",
            path=".",
            recursive=True,
            searchPattern="*.txt",
        ),
    )
    content_search = dispatch(
        app_config,
        FileOperationRequest(
            operation="SEARCH_CONTENT",
            workspace="test",
            path=".",
            searchText="NEEDLE",
            recursive=True,
        ),
    )
    copied = dispatch(
        app_config,
        FileOperationRequest(
            operation="COPY",
            workspace="test",
            path=r"notes\alpha.txt",
            destinationPath=r"archive\copy.txt",
            createParentDirectories=True,
        ),
    )
    moved = dispatch(
        app_config,
        FileOperationRequest(
            operation="MOVE",
            workspace="test",
            path=r"archive\copy.txt",
            destinationPath=r"archive\moved.txt",
        ),
    )
    deleted = dispatch(
        app_config,
        FileOperationRequest(
            operation="DELETE_FILE",
            workspace="test",
            path=r"archive\moved.txt",
        ),
    )

    assert created_directory.itemType == "DIRECTORY"
    assert created_file.itemType == "FILE"
    assert created_file.hash and created_file.hash.startswith("sha256:")
    assert [item.relativePath for item in listed.items] == [
        str(Path("notes") / "alpha.txt")
    ]
    assert name_search.totalResults == 1
    assert content_search.totalResults == 1
    assert content_search.matches[0].matchedText == "needle"
    assert copied.destinationPath == r"archive\copy.txt"
    assert moved.destinationPath == r"archive\moved.txt"
    assert deleted.exists is False
    assert deleted.recycleId
    assert deleted.recyclePath
    assert not (workspace.root / "archive" / "moved.txt").exists()


def test_dispatch_update_append_replace_metadata_and_exists(
    app_config: AppConfig,
) -> None:
    created = dispatch(
        app_config,
        FileOperationRequest(
            operation="CREATE_FILE",
            workspace="test",
            path="versioned.txt",
            content="one",
            returnHash=True,
        ),
    )
    updated = dispatch(
        app_config,
        FileOperationRequest(
            operation="UPDATE_FILE",
            workspace="test",
            path="versioned.txt",
            content="two",
            expectedHash=created.hash,
            returnHash=True,
        ),
    )
    appended = dispatch(
        app_config,
        FileOperationRequest(
            operation="APPEND_FILE",
            workspace="test",
            path="versioned.txt",
            content=" three",
            appendNewLine=False,
            expectedHash=updated.hash,
            returnHash=True,
        ),
    )
    replaced = dispatch(
        app_config,
        FileOperationRequest(
            operation="REPLACE_TEXT",
            workspace="test",
            path="versioned.txt",
            searchText="three",
            replacementText="THREE",
            expectedOccurrences=1,
            expectedHash=appended.hash,
        ),
    )
    metadata = dispatch(
        app_config,
        FileOperationRequest(
            operation="GET_METADATA",
            workspace="test",
            path="versioned.txt",
            returnHash=True,
        ),
    )
    exists = dispatch(
        app_config,
        FileOperationRequest(
            operation="EXISTS",
            workspace="test",
            path="versioned.txt",
        ),
    )

    assert updated.hash != created.hash
    assert appended.hash != updated.hash
    assert replaced.affectedCount == 1
    assert metadata.hash
    assert exists.exists is True
    assert exists.itemType == "FILE"


def test_dispatch_returns_hash_without_other_metadata(
    app_config: AppConfig,
    workspace: Workspace,
) -> None:
    target = workspace.root / "hash-only.txt"
    target.write_text("hash me", encoding="utf-8")

    response = dispatch(
        app_config,
        FileOperationRequest(
            operation="READ_FILE",
            workspace="test",
            path="hash-only.txt",
            returnMetadata=False,
            returnHash=True,
        ),
    )

    assert response.hash and response.hash.startswith("sha256:")
    assert response.name is None
    assert response.extension is None
    assert response.sizeBytes is None
    assert response.createdUtc is None
    assert response.modifiedUtc is None


def test_dispatch_command_maps_nonzero_exit_to_failed_response(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.dispatcher.execute_restricted_command",
        lambda *args, **kwargs: (7, "", "failed", False),
    )

    response = dispatch(
        app_config,
        FileOperationRequest(
            operation="EXECUTE_COMMAND",
            workspace="test",
            shellCommand="Get-ChildItem",
        ),
    )

    assert response.success is False
    assert response.status == Status.FAILED
    assert response.exitCode == 7
    assert response.stderr == "failed"


def test_dispatch_unknown_workspace_fails_closed(
    app_config: AppConfig,
) -> None:
    request = FileOperationRequest(
        operation="EXISTS",
        workspace="unknown",
        path="file.txt",
    )

    with pytest.raises(PolicyViolation) as raised:
        dispatch(app_config, request)

    assert raised.value.code == "WORKSPACE_NOT_FOUND"
