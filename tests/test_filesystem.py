from __future__ import annotations

import base64
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier, BrokenBarrierError

import pytest

import app.filesystem as filesystem_module
from app.config import Workspace
from app.errors import PolicyViolation
from app.filesystem import (
    append_file,
    copy_item,
    create_directory,
    create_file,
    delete_directory,
    delete_file,
    get_metadata,
    list_directory,
    move_item,
    path_exists,
    read_file,
    replace_text,
    sha256_file,
    update_file,
)
from app.models import ItemType


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
        permissions={
            "read": True,
            "create": True,
            "update": True,
            "delete": True,
            "move": True,
            "copy": True,
            "search": True,
        },
        policy={
            "allowed_extensions": [".txt", ".md", ".bin"],
            "maximum_file_size_bytes": 1024,
            "maximum_write_characters": 1024,
            "maximum_search_results": 50,
            "maximum_search_depth": 5,
            "maximum_search_entries": 100,
            "allow_hidden_items": False,
            "allow_reparse_points": False,
            "allow_workspace_root_operation": False,
        },
        command_policy={},
    )


@pytest.mark.parametrize(
    ("encoding", "content"),
    [
        ("utf-8", "สวัสดี"),
        ("utf-8-bom", "BOM text"),
        ("ascii", "plain ASCII"),
        ("unicode", "UTF-16 snowman ☃"),
    ],
)
def test_create_and_read_supported_text_encodings(
    workspace: Workspace,
    encoding: str,
    content: str,
):
    create_file(
        workspace,
        f"{encoding}.txt",
        content,
        encoding,
        overwrite=False,
        create_parents=False,
    )

    _, actual, truncated = read_file(
        workspace,
        f"{encoding}.txt",
        encoding,
        max_characters=100,
    )

    assert actual == content
    assert truncated is False


def test_read_and_append_do_not_use_unbounded_path_reads(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
):
    target = workspace.root / 'bounded.txt'
    target.write_bytes(b'abc')

    def reject_unbounded_read(_path: Path) -> bytes:
        raise AssertionError('Path.read_bytes must not be used for file content')

    monkeypatch.setattr(Path, 'read_bytes', reject_unbounded_read)

    _, content, truncated = read_file(
        workspace,
        'bounded.txt',
        'utf-8',
        max_characters=100,
    )
    append_file(
        workspace,
        'bounded.txt',
        'd',
        'utf-8',
        append_newline=False,
    )

    assert content == 'abc'
    assert truncated is False
    assert target.read_text(encoding='utf-8') == 'abcd'


def test_read_and_append_reject_oversized_existing_file(
    workspace: Workspace,
):
    bounded = replace(
        workspace,
        policy={**workspace.policy, 'maximum_file_size_bytes': 3},
    )
    target = workspace.root / 'oversized.txt'
    target.write_bytes(b'four')

    with pytest.raises(PolicyViolation) as read_error:
        read_file(
            bounded,
            'oversized.txt',
            'utf-8',
            max_characters=100,
        )
    with pytest.raises(PolicyViolation) as append_error:
        append_file(
            bounded,
            'oversized.txt',
            'x',
            'utf-8',
            append_newline=False,
        )

    assert read_error.value.code == 'FILE_SIZE_LIMIT_EXCEEDED'
    assert append_error.value.code == 'FILE_SIZE_LIMIT_EXCEEDED'


@pytest.mark.parametrize('operation', ['update', 'append'])
def test_mutating_writes_revalidate_immediately_before_staging(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
):
    target = workspace.root / 'versioned.txt'
    target.write_text('before', encoding='utf-8')
    resolve_calls: list[str | None] = []
    real_resolve = filesystem_module.resolve_workspace_path
    real_temporary_write = filesystem_module._write_temporary_file

    def tracked_resolve(*args, **kwargs):
        resolve_calls.append(args[1])
        return real_resolve(*args, **kwargs)

    def guarded_temporary_write(path: Path, data: bytes) -> Path:
        assert len(resolve_calls) >= 3
        return real_temporary_write(path, data)

    monkeypatch.setattr(
        filesystem_module,
        'resolve_workspace_path',
        tracked_resolve,
    )
    monkeypatch.setattr(
        filesystem_module,
        '_write_temporary_file',
        guarded_temporary_write,
    )

    if operation == 'update':
        update_file(workspace, 'versioned.txt', 'after', 'utf-8')
    else:
        append_file(
            workspace,
            'versioned.txt',
            ' after',
            'utf-8',
            append_newline=False,
        )


def test_copy_revalidates_destination_before_staging_write(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
):
    source = workspace.root / 'source.txt'
    source.write_text('copy me', encoding='utf-8')
    resolve_calls: list[str | None] = []
    real_resolve = filesystem_module.resolve_workspace_path
    real_copy = filesystem_module.shutil.copy2

    def tracked_resolve(*args, **kwargs):
        resolve_calls.append(args[1])
        return real_resolve(*args, **kwargs)

    def guarded_copy(source_path: Path, staging_path: Path):
        assert len(resolve_calls) >= 4
        return real_copy(source_path, staging_path)

    monkeypatch.setattr(
        filesystem_module,
        'resolve_workspace_path',
        tracked_resolve,
    )
    monkeypatch.setattr(filesystem_module.shutil, 'copy2', guarded_copy)

    copy_item(
        workspace,
        'source.txt',
        'destination.txt',
        overwrite=False,
        recursive=False,
        create_parents=False,
    )


def test_copy_revalidates_destination_after_tree_validation(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
):
    source = workspace.root / 'source.txt'
    source.write_text('copy me', encoding='utf-8')
    events: list[str] = []
    real_resolve = filesystem_module.resolve_workspace_path
    real_validate = filesystem_module._validate_transfer_tree
    real_copy = filesystem_module.shutil.copy2

    def tracked_resolve(*args, **kwargs):
        events.append(f'resolve:{args[1]}')
        return real_resolve(*args, **kwargs)

    def tracked_validate(*args, **kwargs):
        events.append('validate-tree')
        return real_validate(*args, **kwargs)

    def guarded_copy(source_path: Path, staging_path: Path):
        assert events[-1] == 'resolve:destination.txt'
        return real_copy(source_path, staging_path)

    monkeypatch.setattr(
        filesystem_module,
        'resolve_workspace_path',
        tracked_resolve,
    )
    monkeypatch.setattr(
        filesystem_module,
        '_validate_transfer_tree',
        tracked_validate,
    )
    monkeypatch.setattr(filesystem_module.shutil, 'copy2', guarded_copy)

    copy_item(
        workspace,
        'source.txt',
        'destination.txt',
        overwrite=False,
        recursive=False,
        create_parents=False,
    )


def test_move_revalidates_destination_after_tree_validation(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
):
    source = workspace.root / 'source.txt'
    source.write_text('move me', encoding='utf-8')
    events: list[str] = []
    real_resolve = filesystem_module.resolve_workspace_path
    real_validate = filesystem_module._validate_transfer_tree
    real_replace = filesystem_module.os.replace

    def tracked_resolve(*args, **kwargs):
        events.append(f'resolve:{args[1]}')
        return real_resolve(*args, **kwargs)

    def tracked_validate(*args, **kwargs):
        events.append('validate-tree')
        return real_validate(*args, **kwargs)

    def guarded_replace(source_path: Path, destination_path: Path):
        assert events[-1] == 'resolve:destination.txt'
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(
        filesystem_module,
        'resolve_workspace_path',
        tracked_resolve,
    )
    monkeypatch.setattr(
        filesystem_module,
        '_validate_transfer_tree',
        tracked_validate,
    )
    monkeypatch.setattr(filesystem_module.os, 'replace', guarded_replace)
    monkeypatch.setattr(filesystem_module.os, 'rename', guarded_replace)

    move_item(
        workspace,
        'source.txt',
        'destination.txt',
        overwrite=False,
        create_parents=False,
    )


@pytest.mark.parametrize('operation', ['move', 'copy'])
def test_transfer_without_overwrite_uses_atomic_no_replace(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
):
    source = workspace.root / 'source.txt'
    destination = workspace.root / 'destination.txt'
    source.write_text('source', encoding='utf-8')
    real_rename = filesystem_module.os.rename

    def racing_rename(source_path: Path, destination_path: Path):
        destination.write_text('racer', encoding='utf-8')
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(filesystem_module.os, 'rename', racing_rename)

    with pytest.raises(FileExistsError):
        if operation == 'move':
            move_item(
                workspace,
                'source.txt',
                'destination.txt',
                overwrite=False,
                create_parents=False,
            )
        else:
            copy_item(
                workspace,
                'source.txt',
                'destination.txt',
                overwrite=False,
                recursive=False,
                create_parents=False,
            )

    assert destination.read_text(encoding='utf-8') == 'racer'
    assert source.read_text(encoding='utf-8') == 'source'


def test_create_and_read_base64_round_trips_binary_content(workspace: Workspace):
    payload = b"\x00\x01\xfe\xffbinary"
    encoded = base64.b64encode(payload).decode("ascii")

    path = create_file(
        workspace,
        "payload.bin",
        encoded,
        "base64",
        overwrite=False,
        create_parents=False,
    )
    _, actual, truncated = read_file(
        workspace,
        "payload.bin",
        "base64",
        max_characters=100,
    )

    assert path.read_bytes() == payload
    assert actual == encoded
    assert truncated is False


def test_truncated_base64_response_contains_only_complete_quartets(
    workspace: Workspace,
):
    encoded = base64.b64encode(b'abcdef').decode('ascii')
    create_file(
        workspace,
        'payload.bin',
        encoded,
        'base64',
        overwrite=False,
        create_parents=False,
    )

    _, content, truncated = read_file(
        workspace,
        'payload.bin',
        'base64',
        max_characters=5,
    )

    assert truncated is True
    assert len(content) == 4
    assert base64.b64decode(content, validate=True) == b'abc'


def test_invalid_base64_is_rejected(workspace: Workspace):
    with pytest.raises(PolicyViolation) as caught:
        create_file(
            workspace,
            "payload.bin",
            "not!base64",
            "base64",
            overwrite=False,
            create_parents=False,
        )

    assert caught.value.code == "INVALID_BASE64"


def test_create_file_can_create_parents_and_rejects_duplicate(
    workspace: Workspace,
):
    created = create_file(
        workspace,
        r"docs\nested\file.txt",
        "first",
        "utf-8",
        overwrite=False,
        create_parents=True,
    )

    assert created.read_text(encoding="utf-8") == "first"
    with pytest.raises(FileExistsError):
        create_file(
            workspace,
            r"docs\nested\file.txt",
            "second",
            "utf-8",
            overwrite=False,
            create_parents=False,
        )


def test_create_enforces_write_character_and_file_byte_limits(
    workspace: Workspace,
):
    character_limited = replace(
        workspace,
        policy={**workspace.policy, "maximum_write_characters": 3},
    )
    with pytest.raises(PolicyViolation) as character_error:
        create_file(
            character_limited,
            "too-long.txt",
            "four",
            "utf-8",
            overwrite=False,
            create_parents=False,
        )
    assert character_error.value.code == "CONTENT_LIMIT_EXCEEDED"

    byte_limited = replace(
        workspace,
        policy={**workspace.policy, "maximum_file_size_bytes": 3},
    )
    with pytest.raises(PolicyViolation) as byte_error:
        create_file(
            byte_limited,
            "too-large.txt",
            "€€",
            "utf-8",
            overwrite=False,
            create_parents=False,
        )
    assert byte_error.value.code == "FILE_SIZE_LIMIT_EXCEEDED"


def test_read_truncates_response_but_hash_covers_complete_file(
    workspace: Workspace,
):
    target = create_file(
        workspace,
        "long.txt",
        "abcdefgh",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )

    _, content, truncated = read_file(
        workspace,
        "long.txt",
        "utf-8",
        max_characters=3,
    )

    assert content == "abc"
    assert truncated is True
    assert sha256_file(target).startswith("sha256:")
    assert len(sha256_file(target)) == len("sha256:") + 64


def test_update_accepts_current_hash_and_rejects_stale_hash(workspace: Workspace):
    target = create_file(
        workspace,
        "versioned.txt",
        "v1",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )
    current_hash = sha256_file(target)

    update_file(
        workspace,
        "versioned.txt",
        "v2",
        "utf-8",
        expected_hash=current_hash,
        expected_modified=None,
    )

    assert target.read_text(encoding="utf-8") == "v2"
    with pytest.raises(PolicyViolation) as caught:
        update_file(
            workspace,
            "versioned.txt",
            "v3",
            "utf-8",
            expected_hash=current_hash,
            expected_modified=None,
        )
    assert caught.value.code == "HASH_MISMATCH"
    assert target.read_text(encoding="utf-8") == "v2"


def test_explicit_empty_expected_hash_is_not_ignored(workspace: Workspace):
    create_file(
        workspace,
        'hash.txt',
        'content',
        'utf-8',
        overwrite=False,
        create_parents=False,
    )

    with pytest.raises(PolicyViolation) as caught:
        update_file(
            workspace,
            'hash.txt',
            'changed',
            'utf-8',
            expected_hash='',
            expected_modified=None,
        )

    assert caught.value.code == 'HASH_MISMATCH'


def test_concurrent_updates_with_same_hash_cannot_both_succeed(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
):
    target = create_file(
        workspace,
        'concurrent.txt',
        'original',
        'utf-8',
        overwrite=False,
        create_parents=False,
    )
    expected = sha256_file(target)
    actual_replace = os.replace
    rendezvous = Barrier(2)

    def gated_replace(source: Path | str, destination: Path | str) -> None:
        if Path(destination) == target:
            try:
                rendezvous.wait(timeout=0.2)
            except BrokenBarrierError:
                pass
        actual_replace(source, destination)

    monkeypatch.setattr('app.filesystem.os.replace', gated_replace)

    def update(value: str) -> str:
        try:
            update_file(
                workspace,
                'concurrent.txt',
                value,
                'utf-8',
                expected_hash=expected,
                expected_modified=None,
            )
            return 'success'
        except PolicyViolation as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, ['first', 'second']))

    assert sorted(outcomes) == ['HASH_MISMATCH', 'success']


def test_update_checks_last_modified_timestamp(workspace: Workspace):
    target = create_file(
        workspace,
        "timestamp.txt",
        "before",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )
    current = get_metadata(workspace, "timestamp.txt").modifiedUtc
    assert current is not None

    update_file(
        workspace,
        "timestamp.txt",
        "after",
        "utf-8",
        expected_hash=None,
        expected_modified=current,
    )
    with pytest.raises(PolicyViolation) as caught:
        update_file(
            workspace,
            "timestamp.txt",
            "too late",
            "utf-8",
            expected_hash=None,
            expected_modified=current - timedelta(seconds=1),
        )

    assert caught.value.code == "LAST_MODIFIED_MISMATCH"
    assert target.read_text(encoding="utf-8") == "after"


def test_failed_atomic_update_preserves_original_and_cleans_temporary_file(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
):
    target = create_file(
        workspace,
        "atomic.txt",
        "original",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )

    def fail_replace(source: Path | str, destination: Path | str) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("app.filesystem.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated"):
        update_file(
            workspace,
            "atomic.txt",
            "replacement",
            "utf-8",
            expected_hash=None,
            expected_modified=None,
        )

    assert target.read_text(encoding="utf-8") == "original"
    assert list(workspace.root.glob(".atomic.txt.*.tmp")) == []


def test_append_with_and_without_newline_and_without_duplicate_bom(
    workspace: Workspace,
):
    target = create_file(
        workspace,
        "append.txt",
        "first",
        "utf-8-bom",
        overwrite=False,
        create_parents=False,
    )

    append_file(
        workspace,
        "append.txt",
        " second",
        "utf-8-bom",
        append_newline=False,
        expected_hash=sha256_file(target),
    )
    append_file(
        workspace,
        "append.txt",
        " third",
        "utf-8-bom",
        append_newline=True,
        expected_hash=sha256_file(target),
    )

    raw = target.read_bytes()
    assert raw.count(b"\xef\xbb\xbf") == 1
    assert raw.decode("utf-8-sig") == "first second third\r\n"


def test_replace_text_is_case_aware_and_checks_expected_count(
    workspace: Workspace,
):
    target = create_file(
        workspace,
        "replace.txt",
        "Alpha alpha alphabet",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )

    _, replacements = replace_text(
        workspace,
        "replace.txt",
        "alpha",
        "beta",
        encoding="utf-8",
        case_sensitive=False,
        use_regex=False,
        whole_word=True,
        expected_occurrences=2,
        replace_all=True,
        expected_hash=sha256_file(target),
    )

    assert replacements == 2
    assert target.read_text(encoding="utf-8") == "beta beta alphabet"
    with pytest.raises(PolicyViolation) as caught:
        replace_text(
            workspace,
            "replace.txt",
            "beta",
            "gamma",
            encoding="utf-8",
            case_sensitive=True,
            use_regex=False,
            whole_word=True,
            expected_occurrences=3,
            replace_all=True,
            expected_hash=None,
        )
    assert caught.value.code == "UNEXPECTED_MATCH_COUNT"
    assert target.read_text(encoding="utf-8") == "beta beta alphabet"


def test_literal_replace_treats_backslashes_as_content(workspace: Workspace):
    target = create_file(
        workspace,
        'literal.txt',
        'replace me',
        'utf-8',
        overwrite=False,
        create_parents=False,
    )
    _, count = replace_text(
        workspace,
        'literal.txt',
        'replace',
        r'\1 literal',
        encoding='utf-8',
        case_sensitive=True,
        use_regex=False,
        whole_word=False,
        expected_occurrences=1,
        replace_all=False,
        expected_hash=None,
    )
    assert count == 1
    assert target.read_text(encoding='utf-8') == r'\1 literal me'


def test_replace_preflights_expanded_output_before_substitution(
    workspace: Workspace,
):
    bounded = replace(
        workspace,
        policy={**workspace.policy, 'maximum_write_characters': 20},
    )
    target = create_file(
        bounded,
        'expansion.txt',
        'x x x',
        'utf-8',
        overwrite=False,
        create_parents=False,
    )

    with pytest.raises(PolicyViolation) as caught:
        replace_text(
            bounded,
            'expansion.txt',
            'x',
            '0123456789',
            encoding='utf-8',
            case_sensitive=True,
            use_regex=False,
            whole_word=False,
            expected_occurrences=3,
            replace_all=True,
            expected_hash=None,
        )

    assert caught.value.code == 'CONTENT_LIMIT_EXCEEDED'
    assert target.read_text(encoding='utf-8') == 'x x x'


def test_create_list_metadata_and_exists(workspace: Workspace):
    create_directory(
        workspace,
        r"docs\nested",
        create_parents=True,
        overwrite=False,
    )
    create_file(
        workspace,
        r"docs\b.txt",
        "b",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )
    create_file(
        workspace,
        r"docs\a.txt",
        "a",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )

    items, total = list_directory(
        workspace,
        "docs",
        recursive=False,
        include_files=True,
        include_directories=True,
        include_hidden=False,
        max_depth=1,
        max_results=2,
        skip=1,
    )

    assert total == 3
    assert [item.name for item in items] == ["b.txt", "nested"]
    metadata = get_metadata(workspace, r"docs\a.txt", include_hash=True)
    assert metadata.itemType == ItemType.FILE
    assert metadata.hash == sha256_file(workspace.root / "docs" / "a.txt")
    assert path_exists(workspace, r"docs\a.txt") == (True, ItemType.FILE)
    assert path_exists(workspace, r"docs\missing.txt") == (False, None)


def test_move_file_creates_parents_and_never_nests_into_existing_directory(
    workspace: Workspace,
):
    create_file(
        workspace,
        "source.txt",
        "source",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )
    create_file(
        workspace,
        r"archive\destination.txt",
        "old",
        "utf-8",
        overwrite=False,
        create_parents=True,
    )

    destination = move_item(
        workspace,
        "source.txt",
        r"archive\destination.txt",
        overwrite=True,
        create_parents=False,
    )

    assert not (workspace.root / "source.txt").exists()
    assert destination.read_text(encoding="utf-8") == "source"
    assert not (destination / "source.txt").exists()


def test_move_and_copy_reject_incompatible_overwrite_targets(
    workspace: Workspace,
):
    create_file(
        workspace,
        "source.txt",
        "source",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )
    create_directory(
        workspace,
        "destination",
        create_parents=False,
        overwrite=False,
    )

    for operation in (move_item, copy_item):
        with pytest.raises(PolicyViolation) as caught:
            operation(
                workspace,
                "source.txt",
                "destination",
                overwrite=True,
                create_parents=False,
            )
        assert caught.value.code == "INCOMPATIBLE_TARGET_TYPE"


def test_copy_file_preserves_source(workspace: Workspace):
    source = create_file(
        workspace,
        "source.txt",
        "copy me",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )

    destination = copy_item(
        workspace,
        "source.txt",
        r"copies\copy.txt",
        overwrite=False,
        recursive=False,
        create_parents=True,
    )

    assert source.read_text(encoding="utf-8") == "copy me"
    assert destination.read_text(encoding="utf-8") == "copy me"


def test_copy_directory_requires_recursive_and_replaces_instead_of_merging(
    workspace: Workspace,
):
    create_file(
        workspace,
        r"source\new.txt",
        "new",
        "utf-8",
        overwrite=False,
        create_parents=True,
    )
    create_file(
        workspace,
        r"destination\stale.txt",
        "stale",
        "utf-8",
        overwrite=False,
        create_parents=True,
    )

    with pytest.raises(PolicyViolation) as caught:
        copy_item(
            workspace,
            "source",
            "destination",
            overwrite=True,
            recursive=False,
            create_parents=False,
        )
    assert caught.value.code == "RECURSIVE_REQUIRED"

    destination = copy_item(
        workspace,
        "source",
        "destination",
        overwrite=True,
        recursive=True,
        create_parents=False,
    )

    assert (workspace.root / "source" / "new.txt").exists()
    assert (destination / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (destination / "stale.txt").exists()


def test_copy_directory_rejects_destination_inside_source(workspace: Workspace):
    create_file(
        workspace,
        r"source\file.txt",
        "value",
        "utf-8",
        overwrite=False,
        create_parents=True,
    )

    with pytest.raises(PolicyViolation) as caught:
        copy_item(
            workspace,
            "source",
            r"source\nested\copy",
            overwrite=False,
            recursive=True,
            create_parents=True,
        )

    assert caught.value.code == "DESTINATION_INSIDE_SOURCE"


def test_delete_file_moves_it_beneath_recycle_root(workspace: Workspace):
    source = create_file(
        workspace,
        r"docs\delete.txt",
        "recoverable",
        "utf-8",
        overwrite=False,
        create_parents=True,
    )
    expected_hash = sha256_file(source)

    recycle_id, recycled = delete_file(
        workspace,
        r"docs\delete.txt",
        expected_hash=expected_hash,
    )

    assert len(recycle_id) == 32
    assert not source.exists()
    assert recycled.read_text(encoding="utf-8") == "recoverable"
    assert recycled.is_relative_to(workspace.recycle_root)


def test_nonempty_directory_delete_requires_explicit_recursion(
    workspace: Workspace,
):
    create_file(
        workspace,
        r"folder\file.txt",
        "recoverable",
        "utf-8",
        overwrite=False,
        create_parents=True,
    )

    with pytest.raises(PolicyViolation) as caught:
        delete_directory(workspace, "folder", recursive=False)
    assert caught.value.code == "RECURSIVE_REQUIRED"
    assert (workspace.root / "folder" / "file.txt").exists()

    recycle_id, recycled = delete_directory(
        workspace,
        "folder",
        recursive=True,
    )
    assert len(recycle_id) == 32
    assert not (workspace.root / "folder").exists()
    assert (recycled / "file.txt").read_text(encoding="utf-8") == "recoverable"


def test_move_overwrite_recycles_previous_destination(workspace: Workspace):
    create_file(
        workspace,
        "source.txt",
        "new",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )
    create_file(
        workspace,
        "destination.txt",
        "old",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )

    move_item(
        workspace,
        "source.txt",
        "destination.txt",
        overwrite=True,
        create_parents=False,
    )

    recycled_old_versions = [
        path
        for path in workspace.recycle_root.rglob("destination.txt")
        if path.is_file()
    ]
    assert len(recycled_old_versions) == 1
    assert recycled_old_versions[0].read_text(encoding="utf-8") == "old"
    assert (workspace.root / "destination.txt").read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize('operation', [move_item, copy_item])
def test_failed_overwrite_restores_visible_destination(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    operation,
):
    source = create_file(
        workspace,
        'source.txt',
        'new',
        'utf-8',
        overwrite=False,
        create_parents=False,
    )
    destination = create_file(
        workspace,
        'destination.txt',
        'old',
        'utf-8',
        overwrite=False,
        create_parents=False,
    )
    actual_replace = os.replace

    def fail_install(candidate: Path | str, target: Path | str) -> None:
        if Path(target) == destination:
            raise OSError('simulated install failure')
        actual_replace(candidate, target)

    monkeypatch.setattr('app.filesystem.os.replace', fail_install)

    with pytest.raises(OSError, match='simulated install failure'):
        operation(
            workspace,
            'source.txt',
            'destination.txt',
            overwrite=True,
            create_parents=False,
        )

    assert source.read_text(encoding='utf-8') == 'new'
    assert destination.read_text(encoding='utf-8') == 'old'


def test_read_and_update_reject_directory_targets(workspace: Workspace):
    create_directory(
        workspace,
        "folder",
        create_parents=False,
        overwrite=False,
    )

    with pytest.raises(IsADirectoryError):
        read_file(workspace, "folder", "utf-8", max_characters=100)
    with pytest.raises(IsADirectoryError):
        update_file(
            workspace,
            "folder",
            "content",
            "utf-8",
            expected_hash=None,
            expected_modified=None,
        )


def test_append_rejects_stale_hash_without_changing_file(workspace: Workspace):
    target = create_file(
        workspace,
        "append-version.txt",
        "first",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )
    stale = sha256_file(target)
    update_file(
        workspace,
        "append-version.txt",
        "second",
        "utf-8",
        expected_hash=stale,
        expected_modified=None,
    )

    with pytest.raises(PolicyViolation) as caught:
        append_file(
            workspace,
            "append-version.txt",
            " third",
            "utf-8",
            append_newline=False,
            expected_hash=stale,
        )

    assert caught.value.code == "HASH_MISMATCH"
    assert target.read_text(encoding="utf-8") == "second"


def test_create_overwrite_replaces_file_without_leaving_temp_files(
    workspace: Workspace,
):
    target = create_file(
        workspace,
        "overwrite.txt",
        "old",
        "utf-8",
        overwrite=False,
        create_parents=False,
    )

    create_file(
        workspace,
        "overwrite.txt",
        "new",
        "utf-8",
        overwrite=True,
        create_parents=False,
    )

    assert target.read_text(encoding="utf-8") == "new"
    assert [
        path
        for path in workspace.root.iterdir()
        if path.name.startswith(".overwrite.txt.") and path.suffix == ".tmp"
    ] == []
    assert os.path.isfile(target)
