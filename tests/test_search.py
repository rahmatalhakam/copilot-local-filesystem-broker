from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.config import Workspace
from app.errors import PolicyViolation
from app.filesystem import search_content, search_files


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    recycle = tmp_path / "recycle"
    root.mkdir()
    recycle.mkdir()
    return Workspace(
        alias="test",
        root=root,
        recycle_root=recycle,
        permissions={"search": True},
        policy={
            "allowed_extensions": [".txt", ".md"],
            "maximum_file_size_bytes": 4096,
            "maximum_write_characters": 4096,
            "maximum_search_results": 50,
            "maximum_search_depth": 5,
            "maximum_search_entries": 100,
            "allow_hidden_items": False,
            "allow_reparse_points": False,
            "allow_workspace_root_operation": False,
        },
        command_policy={},
    )


def _write(root: Path, relative: str, content: str) -> Path:
    target = root.joinpath(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _search_files(
    workspace: Workspace,
    **overrides,
):
    options = {
        "recursive": True,
        "max_depth": 5,
        "search_pattern": None,
        "name_pattern": None,
        "extension": None,
        "include_files": True,
        "include_directories": False,
        "include_hidden": False,
        "max_results": 50,
        "skip": 0,
    }
    options.update(overrides)
    return search_files(workspace, ".", **options)


def _search_content(
    workspace: Workspace,
    search_text: str,
    **overrides,
):
    options = {
        "recursive": True,
        "max_depth": 5,
        "search_pattern": None,
        "extension": None,
        "case_sensitive": False,
        "use_regex": False,
        "whole_word": False,
        "max_results": 50,
        "skip": 0,
    }
    options.update(overrides)
    return search_content(workspace, ".", search_text, **options)


def test_name_search_honors_case_insensitive_glob_and_extension(
    workspace: Workspace,
):
    _write(workspace.root, "Alpha.TXT", "one")
    _write(workspace.root, "alpha.md", "two")
    _write(workspace.root, "beta.txt", "three")

    items, total = _search_files(
        workspace,
        search_pattern="ALPHA*",
        extension="txt",
    )

    assert total == 1
    assert [item.name for item in items] == ["Alpha.TXT"]


def test_recursive_search_honors_maximum_depth(workspace: Workspace):
    _write(workspace.root, "root.txt", "root")
    _write(workspace.root, "one/first.txt", "one")
    _write(workspace.root, "one/two/second.txt", "two")

    shallow, shallow_total = _search_files(workspace, max_depth=0)
    deeper, deeper_total = _search_files(workspace, max_depth=1)

    assert shallow_total == 1
    assert [item.name for item in shallow] == ["root.txt"]
    assert deeper_total == 2
    assert [item.name for item in deeper] == ["first.txt", "root.txt"]


def test_nonrecursive_search_returns_only_direct_children(workspace: Workspace):
    _write(workspace.root, "direct.txt", "direct")
    _write(workspace.root, "nested/child.txt", "child")

    items, total = _search_files(
        workspace,
        recursive=False,
        include_directories=True,
    )

    assert total == 2
    assert [item.name for item in items] == ["direct.txt", "nested"]


def test_name_search_is_sorted_before_pagination(workspace: Workspace):
    for name in ["e.txt", "a.txt", "d.txt", "b.txt", "c.txt"]:
        _write(workspace.root, name, name)

    items, total = _search_files(workspace, max_results=2, skip=2)

    assert total == 5
    assert [item.name for item in items] == ["c.txt", "d.txt"]


def test_name_search_enforces_workspace_result_cap(workspace: Workspace):
    capped = replace(
        workspace,
        policy={**workspace.policy, "maximum_search_results": 3},
    )
    for index in range(6):
        _write(capped.root, f"{index}.txt", str(index))

    items, total = _search_files(capped, max_results=100)

    assert total == 3
    assert len(items) == 3


def test_search_excludes_hidden_items_unless_both_request_and_policy_allow(
    workspace: Workspace,
):
    _write(workspace.root, ".hidden.txt", "hidden")
    _write(workspace.root, ".hidden-dir/secret.txt", "secret")
    _write(workspace.root, "visible.txt", "visible")

    excluded, _ = _search_files(workspace, include_hidden=True)
    assert [item.name for item in excluded] == ["visible.txt"]

    allowing_workspace = replace(
        workspace,
        policy={**workspace.policy, "allow_hidden_items": True},
    )
    still_excluded, _ = _search_files(
        allowing_workspace,
        include_hidden=False,
    )
    included, _ = _search_files(
        allowing_workspace,
        include_hidden=True,
    )

    assert [item.name for item in still_excluded] == ["visible.txt"]
    assert sorted(item.name for item in included) == [
        ".hidden.txt",
        "secret.txt",
        "visible.txt",
    ]


def test_search_prunes_reparse_point_directories(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
):
    _write(workspace.root, "linked/secret.txt", "secret")
    _write(workspace.root, "visible.txt", "visible")
    monkeypatch.setattr(
        "app.filesystem.is_reparse_point",
        lambda path: path.name == "linked",
    )

    items, total = _search_files(
        workspace,
        include_directories=True,
    )

    assert total == 1
    assert [item.name for item in items] == ["visible.txt"]


def test_search_scan_budget_fails_closed(workspace: Workspace):
    tightly_bounded = replace(
        workspace,
        policy={**workspace.policy, "maximum_search_entries": 2},
    )
    for index in range(3):
        _write(tightly_bounded.root, f"{index}.txt", "value")

    with pytest.raises(PolicyViolation) as caught:
        _search_files(tightly_bounded)

    assert caught.value.code == "SEARCH_SCAN_LIMIT_EXCEEDED"


def test_content_search_is_case_insensitive_with_line_and_column(
    workspace: Workspace,
):
    _write(workspace.root, "notes.txt", "prefix\n  Important VALUE here\n")

    matches, total = _search_content(workspace, "important value")

    assert total == 1
    assert len(matches) == 1
    assert matches[0].relativePath == "notes.txt"
    assert matches[0].lineNumber == 2
    assert matches[0].columnNumber == 3
    assert matches[0].matchedText == "Important VALUE"
    assert matches[0].lineText == "  Important VALUE here"


def test_content_search_honors_whole_word(workspace: Workspace):
    _write(workspace.root, "words.txt", "cat concatenate Cat scat")

    matches, total = _search_content(
        workspace,
        "cat",
        whole_word=True,
    )

    assert total == 2
    assert [match.columnNumber for match in matches] == [1, 17]


def test_content_regex_uses_case_flag_and_accepts_conservative_pattern(
    workspace: Workspace,
):
    _write(workspace.root, "regex.txt", "ALPHA   42\nalpha x\n")

    insensitive, total = _search_content(
        workspace,
        r"alpha\s+\d+",
        use_regex=True,
        case_sensitive=False,
    )
    sensitive, sensitive_total = _search_content(
        workspace,
        r"alpha\s+\d+",
        use_regex=True,
        case_sensitive=True,
    )

    assert total == 1
    assert insensitive[0].matchedText == "ALPHA   42"
    assert sensitive_total == 0
    assert sensitive == []


@pytest.mark.parametrize(
    ("pattern", "code"),
    [
        ("(", "INVALID_REGEX"),
        ("(a+)+$", "UNSAFE_REGEX"),
        (r"(a)\1", "UNSAFE_REGEX"),
        ("(?=secret).*", "UNSAFE_REGEX"),
        ("", "EMPTY_SEARCH_TEXT"),
    ],
)
def test_content_search_rejects_invalid_or_risky_regex_before_scanning(
    workspace: Workspace,
    pattern: str,
    code: str,
):
    _write(workspace.root, "content.txt", "a" * 100)

    with pytest.raises(PolicyViolation) as caught:
        _search_content(workspace, pattern, use_regex=True)

    assert caught.value.code == code


@pytest.mark.parametrize(
    'pattern',
    [
        r'((a+))+$',
        r'(a?){1000}a{1000}',
        r'a+a+$',
        r'(?i)secret',
        r'a?a?a?a?b',
    ],
)
def test_content_search_rejects_regex_safety_bypasses(
    workspace: Workspace,
    pattern: str,
):
    _write(workspace.root, 'content.txt', 'a' * 100)

    with pytest.raises(PolicyViolation) as caught:
        _search_content(workspace, pattern, use_regex=True)

    assert caught.value.code == 'UNSAFE_REGEX'


def test_content_search_bounds_literal_search_text(workspace: Workspace):
    _write(workspace.root, 'content.txt', 'safe')

    with pytest.raises(PolicyViolation) as caught:
        _search_content(workspace, 'a' * 201, use_regex=False)

    assert caught.value.code == 'SEARCH_TEXT_LIMIT_EXCEEDED'


def test_content_search_skips_binary_non_utf8_and_disallowed_extensions(
    workspace: Workspace,
):
    (workspace.root / "binary.txt").write_bytes(b"\x00secret\x00")
    (workspace.root / "invalid.txt").write_bytes(b"\xff\xfe\xfa")
    _write(workspace.root, "source.py", "secret")
    _write(workspace.root, "visible.txt", "secret")

    matches, total = _search_content(workspace, "secret")

    assert total == 1
    assert [match.relativePath for match in matches] == ["visible.txt"]


def test_content_search_enforces_file_size_limit(workspace: Workspace):
    bounded = replace(
        workspace,
        policy={**workspace.policy, "maximum_file_size_bytes": 5},
    )
    _write(bounded.root, "small.txt", "hit")
    _write(bounded.root, "large.txt", "hit but too large")

    matches, total = _search_content(bounded, "hit")

    assert total == 1
    assert [match.relativePath for match in matches] == ["small.txt"]


def test_content_search_paginates_deterministically_and_honors_cap(
    workspace: Workspace,
):
    capped = replace(
        workspace,
        policy={**workspace.policy, "maximum_search_results": 4},
    )
    _write(capped.root, "a.txt", "hit hit")
    _write(capped.root, "b.txt", "hit hit")
    _write(capped.root, "c.txt", "hit hit")

    matches, total = _search_content(
        capped,
        "hit",
        max_results=2,
        skip=1,
    )

    assert total == 4
    assert [
        (match.relativePath, match.columnNumber)
        for match in matches
    ] == [("a.txt", 5), ("b.txt", 1)]


def test_content_search_honors_name_pattern_and_extension(
    workspace: Workspace,
):
    _write(workspace.root, "include.txt", "needle")
    _write(workspace.root, "exclude.txt", "needle")
    _write(workspace.root, "include.md", "needle")

    matches, total = _search_content(
        workspace,
        "needle",
        search_pattern="include*",
        extension=".txt",
    )

    assert total == 1
    assert [match.relativePath for match in matches] == ["include.txt"]
