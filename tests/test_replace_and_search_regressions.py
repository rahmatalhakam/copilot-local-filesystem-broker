"""Regression tests for REPLACE_TEXT and SEARCH_CONTENT fixes.

Each test pins one behaviour that was previously wrong:

* ``replace_all`` no longer collides with the implicit ``expected_occurrences``
  default of 1.
* A count mismatch reports the *real* number of matches and never leaves a
  partially rewritten file behind.
* ``SEARCH_CONTENT`` searches whole files, so multi-line search text works.
* Result-limit truncation is reported instead of silently swallowed.
* UTF-16 files are searchable rather than being treated as binary.
"""

from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from app.config import Workspace
from app.errors import PolicyViolation
from app.filesystem import replace_text, search_content, search_files

SEARCH_DEFAULTS = {
    "recursive": True,
    "max_depth": 5,
    "search_pattern": None,
    "extension": None,
    "case_sensitive": True,
    "use_regex": False,
    "whole_word": False,
    "skip": 0,
}


def _write(workspace: Workspace, name: str, text: str) -> Path:
    target = workspace.root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# REPLACE_TEXT
# --------------------------------------------------------------------------


def test_replace_all_without_expected_occurrences(workspace: Workspace) -> None:
    target = _write(workspace, "all.txt", "alpha alpha alpha")

    _, count = replace_text(
        workspace,
        "all.txt",
        "alpha",
        "beta",
        replace_all=True,
    )

    assert count == 3
    assert target.read_text(encoding="utf-8") == "beta beta beta"


def test_single_replacement_is_still_the_default(workspace: Workspace) -> None:
    _write(workspace, "single.txt", "alpha alpha")

    with pytest.raises(PolicyViolation) as error:
        replace_text(workspace, "single.txt", "alpha", "beta")

    assert error.value.code == "UNEXPECTED_MATCH_COUNT"


def test_mismatch_reports_the_real_match_count(workspace: Workspace) -> None:
    target = _write(workspace, "count.txt", "alpha alpha alpha")

    with pytest.raises(PolicyViolation) as error:
        replace_text(
            workspace,
            "count.txt",
            "alpha",
            "beta",
            expected_occurrences=2,
        )

    assert error.value.code == "UNEXPECTED_MATCH_COUNT"
    assert "found 3" in error.value.message
    # The file must be untouched after a failed assertion.
    assert target.read_text(encoding="utf-8") == "alpha alpha alpha"


def test_expected_occurrences_replaces_every_match(workspace: Workspace) -> None:
    target = _write(workspace, "exact.txt", "alpha alpha alpha")

    _, count = replace_text(
        workspace,
        "exact.txt",
        "alpha",
        "beta",
        expected_occurrences=3,
    )

    assert count == 3
    assert "alpha" not in target.read_text(encoding="utf-8")


def test_zero_matches_is_rejected(workspace: Workspace) -> None:
    target = _write(workspace, "none.txt", "alpha")

    with pytest.raises(PolicyViolation) as error:
        replace_text(
            workspace,
            "none.txt",
            "gamma",
            "beta",
            replace_all=True,
        )

    assert error.value.code == "UNEXPECTED_MATCH_COUNT"
    assert target.read_text(encoding="utf-8") == "alpha"


def test_literal_replacement_is_not_expanded(workspace: Workspace) -> None:
    target = _write(workspace, "literal.txt", "alpha")

    _, count = replace_text(workspace, "literal.txt", "alpha", r"\1\g<0>&")

    assert count == 1
    assert target.read_text(encoding="utf-8") == r"\1\g<0>&"


def test_invalid_regex_replacement_leaves_the_file_intact(
    workspace: Workspace,
) -> None:
    target = _write(workspace, "regex.txt", "alpha")

    with pytest.raises(PolicyViolation) as error:
        replace_text(
            workspace,
            "regex.txt",
            "alpha",
            r"\9",
            use_regex=True,
        )

    assert error.value.code == "INVALID_REGEX_REPLACEMENT"
    assert target.read_text(encoding="utf-8") == "alpha"


# --------------------------------------------------------------------------
# SEARCH_CONTENT / SEARCH_FILES
# --------------------------------------------------------------------------


def test_multi_line_search_text_is_found(workspace: Workspace) -> None:
    _write(workspace, "multi.txt", "first line\nsecond line\nthird line\n")

    matches, total = search_content(
        workspace,
        ".",
        "first line\nsecond line",
        max_results=10,
        **SEARCH_DEFAULTS,
    )

    assert total == 1
    assert matches[0].lineNumber == 1
    assert matches[0].columnNumber == 1
    assert matches[0].lineText == "first line"


def test_every_match_on_a_line_is_reported(workspace: Workspace) -> None:
    _write(workspace, "repeat.txt", "alpha alpha\n")

    matches, total = search_content(
        workspace,
        ".",
        "alpha",
        max_results=10,
        **SEARCH_DEFAULTS,
    )

    assert total == 2
    assert [match.columnNumber for match in matches] == [1, 7]


def test_search_content_reports_truncation(workspace: Workspace) -> None:
    # The result cap comes from the workspace policy, not from max_results.
    workspace.policy["maximum_search_results"] = 3
    _write(workspace, "many.txt", "alpha\n" * 10)

    matches, total, truncated = search_content(
        workspace,
        ".",
        "alpha",
        max_results=10,
        return_truncated=True,
        **SEARCH_DEFAULTS,
    )

    assert truncated is True
    assert total == 3
    assert len(matches) == 3


def test_search_content_without_truncation_flag_is_backwards_compatible(
    workspace: Workspace,
) -> None:
    _write(workspace, "small.txt", "alpha\n")

    result = search_content(
        workspace,
        ".",
        "alpha",
        max_results=10,
        **SEARCH_DEFAULTS,
    )

    assert len(result) == 2


def test_search_files_pages_past_the_result_cap(workspace: Workspace) -> None:
    workspace.policy["maximum_search_results"] = 2
    for index in range(5):
        _write(workspace, f"file{index}.txt", "alpha")

    items, total, truncated = search_files(
        workspace,
        ".",
        recursive=True,
        max_depth=5,
        search_pattern=None,
        name_pattern=None,
        extension=None,
        include_files=True,
        include_directories=False,
        max_results=10,
        skip=0,
        return_truncated=True,
    )

    # B4: the workspace cap bounds each page, never the reachable result
    # set — name searches report the true total and stay fully pageable.
    assert truncated is False
    assert total == 5
    assert len(items) == 2

    tail, tail_total, tail_truncated = search_files(
        workspace,
        ".",
        recursive=True,
        max_depth=5,
        search_pattern=None,
        name_pattern=None,
        extension=None,
        include_files=True,
        include_directories=False,
        max_results=10,
        skip=2,
        return_truncated=True,
    )

    assert tail_truncated is False
    assert tail_total == 5
    assert [item.name for item in tail] == ["file2.txt", "file3.txt"]


def test_utf16_files_are_searchable(workspace: Workspace) -> None:
    target = workspace.root / "utf16.txt"
    target.write_bytes(codecs.BOM_UTF16_LE + "alpha".encode("utf-16-le"))

    matches, total = search_content(
        workspace,
        ".",
        "alpha",
        max_results=10,
        **SEARCH_DEFAULTS,
    )

    assert total == 1
    assert matches[0].relativePath == "utf16.txt"


def test_search_content_accepts_include_hidden(workspace: Workspace) -> None:
    _write(workspace, "visible.txt", "alpha")

    matches, total = search_content(
        workspace,
        ".",
        "alpha",
        max_results=10,
        include_hidden=True,
        **SEARCH_DEFAULTS,
    )

    assert total == 1
    assert matches[0].relativePath == "visible.txt"


def test_multi_line_search_matches_crlf_files(workspace: Workspace) -> None:
    target = workspace.root / "crlf.txt"
    target.write_bytes(b"first line\r\nsecond line\r\nthird line\r\n")

    matches, total = search_content(
        workspace,
        ".",
        "first line\nsecond line",
        max_results=10,
        **SEARCH_DEFAULTS,
    )

    assert total == 1
    assert matches[0].lineNumber == 1
    assert matches[0].lineText == "first line"


def test_replace_multi_line_text_in_a_crlf_file(workspace: Workspace) -> None:
    target = workspace.root / "crlf-replace.txt"
    target.write_bytes(b"first line\r\nsecond line\r\nthird line\r\n")

    _, count = replace_text(
        workspace,
        "crlf-replace.txt",
        "first line\nsecond line",
        "merged line\nnew line",
    )

    assert count == 1
    # The CRLF line endings of the file must survive the edit.
    assert target.read_bytes() == b"merged line\r\nnew line\r\nthird line\r\n"


def test_crlf_search_text_matches_an_lf_file(workspace: Workspace) -> None:
    target = workspace.root / "lf.txt"
    target.write_bytes(b"first line\nsecond line\n")

    _, count = replace_text(
        workspace,
        "lf.txt",
        "first line\r\nsecond line",
        "merged",
    )

    assert count == 1
    assert target.read_bytes() == b"merged\n"

# --------------------------------------------------------------------------
# expected_occurrences contract
# --------------------------------------------------------------------------


def test_expected_occurrences_below_one_is_rejected(
    workspace: Workspace,
) -> None:
    target = _write(workspace, "guard.txt", "alpha alpha\n")

    with pytest.raises(PolicyViolation) as error:
        replace_text(
            workspace,
            "guard.txt",
            "alpha",
            "beta",
            expected_occurrences=0,
            replace_all=True,
        )

    assert error.value.code == "INVALID_EXPECTED_OCCURRENCES"
    assert target.read_text(encoding="utf-8") == "alpha alpha\n"


def test_request_schema_defaults_expected_occurrences_to_unset() -> None:
    from pydantic import ValidationError

    from app.models import FileOperationRequest

    request = FileOperationRequest(operation="REPLACE_TEXT", workspace="w")
    assert request.expectedOccurrences is None

    with pytest.raises(ValidationError):
        FileOperationRequest(
            operation="REPLACE_TEXT",
            workspace="w",
            expectedOccurrences=0,
        )


# --------------------------------------------------------------------------
# Regex line endings + actionable mismatch hints (batch 4)
# --------------------------------------------------------------------------


def test_regex_newline_replaces_in_a_crlf_file(workspace: Workspace) -> None:
    target = workspace.root / "crlf-regex.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    _, count = replace_text(
        workspace,
        "crlf-regex.txt",
        r"alpha\nbeta",
        "one\ntwo",
        use_regex=True,
    )

    assert count == 1
    # `\n` in the pattern matches CRLF, and the endings survive the edit.
    assert target.read_bytes() == b"one\r\ntwo\r\ngamma\r\n"


def test_regex_newline_search_matches_a_crlf_file(
    workspace: Workspace,
) -> None:
    target = workspace.root / "crlf-regex-search.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")

    settings = dict(SEARCH_DEFAULTS)
    settings["use_regex"] = True
    matches, total = search_content(
        workspace,
        ".",
        r"alpha\nbeta",
        max_results=10,
        **settings,
    )

    assert total == 1
    assert matches[0].lineNumber == 1
    assert matches[0].lineText == "alpha"


def test_mismatch_message_suggests_the_actual_count(
    workspace: Workspace,
) -> None:
    _write(workspace, "hint.txt", "alpha alpha alpha")

    with pytest.raises(PolicyViolation) as error:
        replace_text(workspace, "hint.txt", "alpha", "beta")

    assert error.value.code == "UNEXPECTED_MATCH_COUNT"
    assert "found 3" in error.value.message
    # The message tells the caller exactly how to resolve the mismatch.
    assert "expectedOccurrences=3" in error.value.message


def test_zero_match_message_says_the_text_was_not_found(
    workspace: Workspace,
) -> None:
    _write(workspace, "missing.txt", "alpha")

    with pytest.raises(PolicyViolation) as error:
        replace_text(workspace, "missing.txt", "gamma", "beta")

    assert error.value.code == "UNEXPECTED_MATCH_COUNT"
    assert "not found" in error.value.message

# --------------------------------------------------------------------------
# R3: caseSensitive defaults to True
# --------------------------------------------------------------------------


def test_replace_is_case_sensitive_by_default(workspace: Workspace) -> None:
    target = _write(workspace, "casing.txt", "Alpha alpha\n")

    _, count = replace_text(workspace, "casing.txt", "alpha", "beta")

    assert count == 1
    assert target.read_text(encoding="utf-8") == "Alpha beta\n"


def test_replace_can_opt_out_of_case_sensitivity(workspace: Workspace) -> None:
    target = _write(workspace, "casing_opt_out.txt", "Alpha alpha\n")

    _, count = replace_text(
        workspace,
        "casing_opt_out.txt",
        "alpha",
        "beta",
        case_sensitive=False,
        replace_all=True,
    )

    assert count == 2
    assert target.read_text(encoding="utf-8") == "beta beta\n"


def test_request_model_defaults_to_case_sensitive() -> None:
    from app.models import FileOperationRequest, Operation

    request = FileOperationRequest(
        operation=Operation.REPLACE_TEXT,
        workspace="demo",
        path="example.txt",
        searchText="alpha",
        replacementText="beta",
    )

    assert request.caseSensitive is True


def test_default_case_sensitive_does_not_invalidate_other_operations() -> None:
    from app.dispatcher import validate_operation_request
    from app.errors import RequestValidationError
    from app.models import FileOperationRequest, Operation

    # The implicit default must not trip field-applicability validation.
    validate_operation_request(
        FileOperationRequest(
            operation=Operation.READ_FILE,
            workspace="demo",
            path="example.txt",
        )
    )

    # An explicitly provided value on an inapplicable operation still fails.
    with pytest.raises(RequestValidationError):
        validate_operation_request(
            FileOperationRequest.model_validate(
                {
                    "operation": "READ_FILE",
                    "workspace": "demo",
                    "path": "example.txt",
                    "caseSensitive": True,
                }
            )
        )
