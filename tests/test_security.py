from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import app.security as security
from app.config import Workspace
from app.errors import PolicyViolation
from app.security import resolve_workspace_path, validate_extension


@pytest.mark.parametrize(
    ("value", "expected_code"),
    [
        (r"C:\Windows\System32", "ABSOLUTE_PATH_DENIED"),
        (r"D:relative.txt", "ABSOLUTE_PATH_DENIED"),
        (r"\Windows\System32", "ABSOLUTE_PATH_DENIED"),
        (r"/Windows/System32", "ABSOLUTE_PATH_DENIED"),
        (r"\\server\share\file.txt", "UNC_PATH_DENIED"),
        (r"//server/share/file.txt", "UNC_PATH_DENIED"),
        (r"..\outside.txt", "PATH_TRAVERSAL_DENIED"),
        (r"folder\..\outside.txt", "PATH_TRAVERSAL_DENIED"),
        (r"folder/../outside.txt", "PATH_TRAVERSAL_DENIED"),
        (r"file.txt:hidden", "ALTERNATE_DATA_STREAM_DENIED"),
    ],
)
def test_rejects_unsafe_windows_paths(
    workspace: Workspace,
    value: str,
    expected_code: str,
) -> None:
    with pytest.raises(PolicyViolation) as exc_info:
        resolve_workspace_path(workspace, value)

    assert exc_info.value.code == expected_code
    assert exc_info.value.rule


@pytest.mark.parametrize("value", [None, "", "   ", ".", r".\\"])
def test_rejects_workspace_root_by_default(
    workspace: Workspace,
    value: str | None,
) -> None:
    with pytest.raises(PolicyViolation) as exc_info:
        resolve_workspace_path(workspace, value)

    assert exc_info.value.code == "WORKSPACE_ROOT_OPERATION_DENIED"


def test_accepts_normal_nested_windows_path(workspace: Workspace) -> None:
    result = resolve_workspace_path(workspace, r"docs\example.txt")

    assert result == workspace.root / "docs" / "example.txt"


def test_accepts_workspace_root_only_when_explicit(
    workspace: Workspace,
) -> None:
    result = resolve_workspace_path(workspace, ".", allow_root=True)

    assert result == workspace.root


def test_must_exist_raises_file_not_found(workspace: Workspace) -> None:
    missing = workspace.root / "missing.txt"

    with pytest.raises(FileNotFoundError) as exc_info:
        resolve_workspace_path(workspace, "missing.txt", must_exist=True)

    assert exc_info.value.filename == str(missing)


@pytest.mark.parametrize(
    "value",
    [".secret.txt", r"docs\.hidden\file.txt"],
)
def test_hidden_path_components_are_denied(
    workspace: Workspace,
    value: str,
) -> None:
    with pytest.raises(PolicyViolation) as exc_info:
        resolve_workspace_path(workspace, value)

    assert exc_info.value.code == "HIDDEN_ITEM_DENIED"


def test_existing_hidden_ancestor_is_denied(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = workspace.root / "hidden"
    hidden.mkdir()
    monkeypatch.setattr(
        security,
        "is_hidden",
        lambda path: path == hidden,
    )

    with pytest.raises(PolicyViolation) as exc_info:
        resolve_workspace_path(workspace, r"hidden\file.txt")

    assert exc_info.value.code == "HIDDEN_ITEM_DENIED"


def test_existing_reparse_ancestor_is_denied(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    junction = workspace.root / "junction"
    junction.mkdir()
    monkeypatch.setattr(
        security,
        "is_reparse_point",
        lambda path: path == junction,
    )

    with pytest.raises(PolicyViolation) as exc_info:
        resolve_workspace_path(workspace, r"junction\file.txt")

    assert exc_info.value.code == "REPARSE_POINT_DENIED"


def test_reparse_ancestor_follows_explicit_workspace_policy(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    junction = workspace.root / "junction"
    junction.mkdir()
    monkeypatch.setattr(
        security,
        "is_reparse_point",
        lambda path: path == junction,
    )
    permissive = replace(
        workspace,
        policy={**workspace.policy, "allow_reparse_points": True},
    )

    result = resolve_workspace_path(permissive, r"junction\file.txt")

    assert result == junction / "file.txt"


def test_workspace_root_reparse_point_is_denied(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        security,
        "is_reparse_point",
        lambda path: path == workspace.root,
    )

    with pytest.raises(PolicyViolation) as exc_info:
        resolve_workspace_path(workspace, "file.txt")

    assert exc_info.value.code == "REPARSE_POINT_DENIED"


@pytest.mark.parametrize(
    "value",
    ["CON", "con.txt", r"directory\LPT1.log", "COM9.md"],
)
def test_rejects_reserved_windows_device_names(
    workspace: Workspace,
    value: str,
) -> None:
    with pytest.raises(PolicyViolation) as exc_info:
        resolve_workspace_path(workspace, value)

    assert exc_info.value.code == "WINDOWS_DEVICE_PATH_DENIED"


@pytest.mark.parametrize(
    "value",
    ["trailing.", "trailing ", "bad?.txt", "nul\x00byte.txt"],
)
def test_rejects_ambiguous_or_invalid_windows_syntax(
    workspace: Workspace,
    value: str,
) -> None:
    with pytest.raises(PolicyViolation) as exc_info:
        resolve_workspace_path(workspace, value)

    assert exc_info.value.code == "WINDOWS_PATH_SYNTAX_DENIED"


def test_validate_extension_is_case_insensitive(
    workspace: Workspace,
) -> None:
    validate_extension(workspace, workspace.root / "README.TXT")


@pytest.mark.parametrize("name", ["payload.exe", "README", "file."])
def test_validate_extension_rejects_non_allowlisted_files(
    workspace: Workspace,
    name: str,
) -> None:
    with pytest.raises(PolicyViolation) as exc_info:
        validate_extension(workspace, workspace.root / name)

    assert exc_info.value.code == "EXTENSION_DENIED"
