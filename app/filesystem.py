from __future__ import annotations

import base64
import binascii
import codecs
import fnmatch
import hashlib
import hmac
import os
import re
import shutil
import stat
import uuid
from bisect import bisect_right
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from app.config import Workspace
from app.errors import PolicyViolation
from app.models import ContentMatch, FileSystemItem, ItemType
from app.security import (
    is_hidden,
    is_reparse_point,
    resolve_workspace_path,
    validate_extension,
)


_TEXT_CODECS = {
    "utf-8": "utf-8",
    "utf-8-bom": "utf-8-sig",
    "ascii": "ascii",
    "unicode": "utf-16",
}
_DEFAULT_MAXIMUM_FILE_SIZE = 5 * 1024 * 1024
_DEFAULT_MAXIMUM_WRITE_CHARACTERS = 1_000_000
_DEFAULT_MAXIMUM_SEARCH_RESULTS = 500
_DEFAULT_MAXIMUM_SEARCH_DEPTH = 20
_DEFAULT_MAXIMUM_SEARCH_ENTRIES = 10_000
_MAXIMUM_REGEX_CHARACTERS = 200
_MAXIMUM_MATCH_TEXT_CHARACTERS = 1_000
_PATH_LOCKS = tuple(RLock() for _ in range(128))


def _violation(code: str, message: str, rule: str) -> PolicyViolation:
    return PolicyViolation(code, message, rule)


def _policy_int(workspace: Workspace, name: str, default: int) -> int:
    value = int(workspace.policy.get(name, default))
    if value < 0:
        raise _violation(
            "INVALID_POLICY",
            f"Workspace policy '{name}' cannot be negative.",
            "validated-workspace-policy",
        )
    return value


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _lock_index(path: Path) -> int:
    key = os.path.normcase(os.path.abspath(path))
    return hash(key) % len(_PATH_LOCKS)


@contextmanager
def _locked_paths(*paths: Path) -> Iterator[None]:
    locks = [_PATH_LOCKS[index] for index in sorted({_lock_index(path) for path in paths})]
    for lock in locks:
        lock.acquire()
    try:
        yield
    finally:
        for lock in reversed(locks):
            lock.release()


def utc_from_timestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def item_type(path: Path) -> ItemType:
    if is_reparse_point(path):
        return ItemType.REPARSE_POINT
    if path.is_file():
        return ItemType.FILE
    if path.is_dir():
        return ItemType.DIRECTORY
    return ItemType.OTHER


def make_item(
    workspace: Workspace,
    path: Path,
    include_hash: bool = False,
) -> FileSystemItem:
    kind = item_type(path)
    stats = path.lstat() if kind == ItemType.REPARSE_POINT else path.stat()
    is_file = kind == ItemType.FILE
    readonly_attribute = bool(
        getattr(stats, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0)
    )
    return FileSystemItem(
        name=path.name,
        relativePath=str(path.relative_to(workspace.root)),
        itemType=kind,
        extension=path.suffix or None,
        sizeBytes=stats.st_size if is_file else None,
        createdUtc=utc_from_timestamp(stats.st_ctime),
        modifiedUtc=utc_from_timestamp(stats.st_mtime),
        isHidden=is_hidden(path),
        isReadOnly=readonly_attribute or not os.access(path, os.W_OK),
        hash=sha256_file(path) if include_hash and is_file else None,
    )


def _codec_for(encoding: str) -> str:
    try:
        return _TEXT_CODECS[encoding]
    except KeyError as exc:
        raise _violation(
            "UNSUPPORTED_ENCODING",
            f"Encoding '{encoding}' is not supported.",
            "allowlisted-content-encoding",
        ) from exc


def decode_request_content(content: str, encoding: str) -> bytes:
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _violation(
                "INVALID_BASE64",
                "The supplied content is not valid Base64.",
                "validate-content-encoding",
            ) from exc

    try:
        return content.encode(_codec_for(encoding))
    except UnicodeEncodeError as exc:
        raise _violation(
            "ENCODING_ERROR",
            f"The supplied content cannot be encoded as {encoding}.",
            "validate-content-encoding",
        ) from exc


def encode_response_content(data: bytes, encoding: str) -> str:
    if encoding == "base64":
        return base64.b64encode(data).decode("ascii")

    try:
        return data.decode(_codec_for(encoding))
    except UnicodeDecodeError as exc:
        raise _violation(
            "DECODING_ERROR",
            f"The file is not valid {encoding} content.",
            "validate-content-encoding",
        ) from exc


def _ensure_write_character_limit(
    workspace: Workspace,
    content: str,
) -> None:
    maximum = _policy_int(
        workspace,
        "maximum_write_characters",
        _DEFAULT_MAXIMUM_WRITE_CHARACTERS,
    )
    if len(content) > maximum:
        raise _violation(
            "CONTENT_LIMIT_EXCEEDED",
            "The supplied content exceeds the configured character limit.",
            "maximum-write-characters",
        )


def _ensure_file_size_limit(
    workspace: Workspace,
    byte_count: int,
    *,
    action: str,
) -> None:
    maximum = _policy_int(
        workspace,
        "maximum_file_size_bytes",
        _DEFAULT_MAXIMUM_FILE_SIZE,
    )
    if byte_count > maximum:
        raise _violation(
            "FILE_SIZE_LIMIT_EXCEEDED",
            f"The file exceeds the configured {action} size limit.",
            "maximum-file-size",
        )


def _read_bounded_file_bytes(
    workspace: Workspace,
    target: Path,
    *,
    action: str,
) -> bytes:
    maximum = _policy_int(
        workspace,
        'maximum_file_size_bytes',
        _DEFAULT_MAXIMUM_FILE_SIZE,
    )
    if is_reparse_point(target):
        raise _violation(
            'REPARSE_POINT_DENIED',
            'The file changed to a reparse point before it was read.',
            'deny-reparse-point',
        )

    with target.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        current = os.stat(target, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino)
            != (current.st_dev, current.st_ino)
            or is_reparse_point(target)
        ):
            raise _violation(
                'FILE_CHANGED_DURING_READ',
                'The file changed while it was being opened.',
                'stable-file-handle',
            )

        _ensure_file_size_limit(workspace, opened.st_size, action=action)
        data = stream.read(maximum + 1)
        final_opened = os.fstat(stream.fileno())
        _ensure_file_size_limit(workspace, final_opened.st_size, action=action)
        _ensure_file_size_limit(workspace, len(data), action=action)

        final_current = os.stat(target, follow_symlinks=False)
        if (
            (opened.st_dev, opened.st_ino)
            != (final_current.st_dev, final_current.st_ino)
            or is_reparse_point(target)
        ):
            raise _violation(
                'FILE_CHANGED_DURING_READ',
                'The file changed while it was being read.',
                'stable-file-handle',
            )
    return data


def _ensure_file_target(workspace: Workspace, target: Path) -> None:
    if not target.is_file() or is_reparse_point(target):
        if target.is_dir():
            raise IsADirectoryError(target)
        raise _violation(
            "FILE_TARGET_REQUIRED",
            "The operation requires a regular file target.",
            "regular-file-target",
        )
    validate_extension(workspace, target)


def _normalize_expected_modified(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def check_expected_version(
    path: Path,
    expected_hash: str | None,
    expected_modified: datetime | None,
) -> None:
    if expected_hash is not None:
        actual_hash = sha256_file(path)
        if not hmac.compare_digest(actual_hash.casefold(), expected_hash.casefold()):
            raise _violation(
                "HASH_MISMATCH",
                "The file changed after it was read.",
                "optimistic-concurrency-hash",
            )

    if expected_modified:
        actual = utc_from_timestamp(path.stat().st_mtime)
        expected = _normalize_expected_modified(expected_modified)
        if abs((actual - expected).total_seconds()) > 0.001:
            raise _violation(
                "LAST_MODIFIED_MISMATCH",
                "The file modification timestamp has changed.",
                "optimistic-concurrency-timestamp",
            )


def _write_temporary_file(target: Path, data: bytes) -> Path:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if _path_exists(temporary):
            temporary.unlink()
        raise
    return temporary


def _atomic_replace_bytes(target: Path, data: bytes) -> None:
    temporary = _write_temporary_file(target, data)
    try:
        os.replace(temporary, target)
    finally:
        if _path_exists(temporary):
            temporary.unlink()


def _atomic_create_bytes(target: Path, data: bytes) -> None:
    temporary = _write_temporary_file(target, data)
    try:
        os.link(temporary, target)
    finally:
        if _path_exists(temporary):
            temporary.unlink()


def _create_file_unlocked(
    workspace: Workspace,
    relative_path: str,
    content: str,
    encoding: str = "utf-8",
    overwrite: bool = False,
    create_parents: bool = False,
) -> Path:
    target = resolve_workspace_path(workspace, relative_path)
    validate_extension(workspace, target)
    _ensure_write_character_limit(workspace, content)
    data = decode_request_content(content, encoding)
    _ensure_file_size_limit(workspace, len(data), action="write")

    if _path_exists(target):
        if not overwrite:
            raise FileExistsError(target)
        if not target.is_file() or is_reparse_point(target):
            raise IsADirectoryError(target)

    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.is_dir():
        raise FileNotFoundError(target.parent)

    target = resolve_workspace_path(workspace, relative_path)
    validate_extension(workspace, target)

    if overwrite:
        _atomic_replace_bytes(target, data)
    else:
        _atomic_create_bytes(target, data)
    return target


def create_file(
    workspace: Workspace,
    relative_path: str,
    content: str,
    encoding: str = 'utf-8',
    overwrite: bool = False,
    create_parents: bool = False,
) -> Path:
    target = resolve_workspace_path(workspace, relative_path)
    with _locked_paths(target):
        return _create_file_unlocked(
            workspace,
            relative_path,
            content,
            encoding,
            overwrite,
            create_parents,
        )


def read_file(
    workspace: Workspace,
    relative_path: str,
    encoding: str = "utf-8",
    max_characters: int = 100_000,
) -> tuple[Path, str, bool]:
    if max_characters < 0:
        raise _violation(
            "INVALID_LIMIT",
            "max_characters cannot be negative.",
            "bounded-content-response",
        )
    target = resolve_workspace_path(
        workspace,
        relative_path,
        must_exist=True,
    )
    _ensure_file_target(workspace, target)
    data = _read_bounded_file_bytes(workspace, target, action='read')
    size = len(data)
    _ensure_file_size_limit(workspace, size, action="read")

    value = encode_response_content(data, encoding)
    truncated = len(value) > max_characters
    response_limit = max_characters
    if encoding == 'base64':
        response_limit -= response_limit % 4
    return target, value[:response_limit], truncated


def _update_file_unlocked(
    workspace: Workspace,
    relative_path: str,
    content: str,
    encoding: str = "utf-8",
    expected_hash: str | None = None,
    expected_modified: datetime | None = None,
) -> Path:
    target = resolve_workspace_path(
        workspace,
        relative_path,
        must_exist=True,
    )
    _ensure_file_target(workspace, target)
    _ensure_write_character_limit(workspace, content)
    data = decode_request_content(content, encoding)
    _ensure_file_size_limit(workspace, len(data), action="write")
    check_expected_version(target, expected_hash, expected_modified)

    target = resolve_workspace_path(
        workspace,
        relative_path,
        must_exist=True,
    )
    _ensure_file_target(workspace, target)
    check_expected_version(target, expected_hash, expected_modified)
    temporary = _write_temporary_file(target, data)
    try:
        target = resolve_workspace_path(workspace, relative_path, must_exist=True)
        _ensure_file_target(workspace, target)
        check_expected_version(target, expected_hash, expected_modified)
        os.replace(temporary, target)
    finally:
        if _path_exists(temporary):
            temporary.unlink()
    return target


def update_file(
    workspace: Workspace,
    relative_path: str,
    content: str,
    encoding: str = 'utf-8',
    expected_hash: str | None = None,
    expected_modified: datetime | None = None,
) -> Path:
    target = resolve_workspace_path(workspace, relative_path, must_exist=True)
    with _locked_paths(target):
        return _update_file_unlocked(
            workspace,
            relative_path,
            content,
            encoding,
            expected_hash,
            expected_modified,
        )


def _append_file_unlocked(
    workspace: Workspace,
    relative_path: str,
    content: str,
    encoding: str = "utf-8",
    append_newline: bool = True,
    expected_hash: str | None = None,
) -> Path:
    target = resolve_workspace_path(
        workspace,
        relative_path,
        must_exist=True,
    )
    _ensure_file_target(workspace, target)
    check_expected_version(target, expected_hash, None)
    existing_bytes = _read_bounded_file_bytes(
        workspace,
        target,
        action='append',
    )

    if encoding == "base64":
        _ensure_write_character_limit(workspace, content)
        appended = decode_request_content(content, encoding)
        if append_newline:
            appended += b"\r\n"
        data = existing_bytes + appended
    else:
        existing = encode_response_content(existing_bytes, encoding)
        updated = existing + content + ("\r\n" if append_newline else "")
        _ensure_write_character_limit(workspace, updated)
        data = decode_request_content(updated, encoding)

    _ensure_file_size_limit(workspace, len(data), action="write")
    target = resolve_workspace_path(
        workspace,
        relative_path,
        must_exist=True,
    )
    _ensure_file_target(workspace, target)
    check_expected_version(target, expected_hash, None)
    temporary = _write_temporary_file(target, data)
    try:
        target = resolve_workspace_path(workspace, relative_path, must_exist=True)
        _ensure_file_target(workspace, target)
        check_expected_version(target, expected_hash, None)
        os.replace(temporary, target)
    finally:
        if _path_exists(temporary):
            temporary.unlink()
    return target


def append_file(
    workspace: Workspace,
    relative_path: str,
    content: str,
    encoding: str = 'utf-8',
    append_newline: bool = True,
    expected_hash: str | None = None,
) -> Path:
    target = resolve_workspace_path(workspace, relative_path, must_exist=True)
    with _locked_paths(target):
        return _append_file_unlocked(
            workspace,
            relative_path,
            content,
            encoding,
            append_newline,
            expected_hash,
        )


def _replace_text_unlocked(
    workspace: Workspace,
    relative_path: str,
    search_text: str,
    replacement_text: str,
    *,
    encoding: str = "utf-8",
    case_sensitive: bool = False,
    use_regex: bool = False,
    whole_word: bool = False,
    expected_occurrences: int | None = None,
    replace_all: bool = False,
    expected_hash: str | None = None,
) -> tuple[Path, int]:
    if encoding == "base64":
        raise _violation(
            "TEXT_ENCODING_REQUIRED",
            "Text replacement is not available for Base64 content.",
            "text-operation-encoding",
        )
    maximum = _policy_int(
        workspace,
        "maximum_write_characters",
        _DEFAULT_MAXIMUM_WRITE_CHARACTERS,
    )
    target, text, truncated = read_file(
        workspace,
        relative_path,
        encoding,
        maximum,
    )
    if truncated:
        raise _violation(
            "CONTENT_LIMIT_EXCEEDED",
            "The file is too large for text replacement.",
            "maximum-write-characters",
        )
    check_expected_version(target, expected_hash, None)
    if expected_occurrences is None and not replace_all:
        # A single targeted replacement stays assertive by default.
        expected_occurrences = 1
    pattern = _compile_pattern(
        search_text,
        case_sensitive=case_sensitive,
        use_regex=use_regex,
        whole_word=whole_word,
    )
    # Every match is counted: the file is already bounded by
    # maximum_write_characters, and an accurate count is what makes the
    # mismatch message actionable.
    matches = list(pattern.finditer(text))
    count = len(matches)
    if expected_occurrences is not None and count != expected_occurrences:
        raise _violation(
            "UNEXPECTED_MATCH_COUNT",
            f"Expected {expected_occurrences} match(es), found {count}.",
            "expected-replacement-occurrences",
        )

    if count == 0:
        raise _violation(
            "UNEXPECTED_MATCH_COUNT",
            "Expected at least 1 match(es), found 0.",
            "expected-replacement-occurrences",
        )

    # Never write a partial edit while reporting success: the caller either
    # replaces every match (replace_all) or exactly the asserted number of
    # matches, which the guard above has already proven equals `count`.
    replacement_count = count
    projected_size = len(text)
    file_uses_crlf = '\r\n' in text
    try:
        for found in matches[:replacement_count]:
            expanded = _align_newlines(
                found.expand(replacement_text)
                if use_regex
                else replacement_text,
                file_uses_crlf,
            )
            projected_size += len(expanded) - len(found.group(0))
        if projected_size > maximum:
            raise _violation(
                'CONTENT_LIMIT_EXCEEDED',
                'Replacement output exceeds the configured limit.',
                'maximum-write-characters',
            )
        if use_regex:
            def replacement(match: re.Match[str]) -> str:
                return _align_newlines(
                    match.expand(replacement_text),
                    file_uses_crlf,
                )
        else:
            def replacement(_match: re.Match[str]) -> str:
                return _align_newlines(replacement_text, file_uses_crlf)

        updated, replacements = pattern.subn(
            replacement,
            text,
            count=replacement_count,
        )
    except re.error as exc:
        raise _violation(
            "INVALID_REGEX_REPLACEMENT",
            "The replacement text is invalid for the regular expression.",
            "safe-regular-expression",
        ) from exc

    update_file(
        workspace,
        relative_path,
        updated,
        encoding,
        expected_hash,
        None,
    )
    return target, replacements


def replace_text(
    workspace: Workspace,
    relative_path: str,
    search_text: str,
    replacement_text: str,
    *,
    encoding: str = 'utf-8',
    case_sensitive: bool = False,
    use_regex: bool = False,
    whole_word: bool = False,
    expected_occurrences: int | None = None,
    replace_all: bool = False,
    expected_hash: str | None = None,
) -> tuple[Path, int]:
    target = resolve_workspace_path(workspace, relative_path, must_exist=True)
    with _locked_paths(target):
        return _replace_text_unlocked(
            workspace,
            relative_path,
            search_text,
            replacement_text,
            encoding=encoding,
            case_sensitive=case_sensitive,
            use_regex=use_regex,
            whole_word=whole_word,
            expected_occurrences=expected_occurrences,
            replace_all=replace_all,
            expected_hash=expected_hash,
        )


def get_metadata(
    workspace: Workspace,
    relative_path: str,
    include_hash: bool = False,
) -> FileSystemItem:
    target = resolve_workspace_path(
        workspace,
        relative_path,
        must_exist=True,
        allow_root=True,
    )
    return make_item(workspace, target, include_hash)


def path_exists(
    workspace: Workspace,
    relative_path: str,
) -> tuple[bool, ItemType | None]:
    target = resolve_workspace_path(
        workspace,
        relative_path,
        allow_root=True,
    )
    if not _path_exists(target):
        return False, None
    return True, item_type(target)


def create_directory(
    workspace: Workspace,
    relative_path: str,
    create_parents: bool = False,
    overwrite: bool = False,
) -> Path:
    target = resolve_workspace_path(workspace, relative_path)
    if _path_exists(target):
        if not overwrite:
            raise FileExistsError(target)
        if not target.is_dir() or is_reparse_point(target):
            raise NotADirectoryError(target)
        return target

    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.is_dir():
        raise FileNotFoundError(target.parent)

    target = resolve_workspace_path(workspace, relative_path)
    target.mkdir(parents=False, exist_ok=False)
    target = resolve_workspace_path(
        workspace,
        relative_path,
        must_exist=True,
    )
    if not target.is_dir() or is_reparse_point(target):
        raise NotADirectoryError(target)
    return target


def list_directory(
    workspace: Workspace,
    relative_path: str,
    *,
    recursive: bool = False,
    include_files: bool = True,
    include_directories: bool = False,
    include_hidden: bool = False,
    max_depth: int = 10,
    max_results: int = 100,
    skip: int = 0,
) -> tuple[list[FileSystemItem], int]:
    return search_files(
        workspace,
        relative_path,
        recursive=recursive,
        max_depth=max_depth,
        search_pattern=None,
        name_pattern=None,
        extension=None,
        include_files=include_files,
        include_directories=include_directories,
        include_hidden=include_hidden,
        max_results=max_results,
        skip=skip,
    )


def _relative_to_workspace(workspace: Workspace, path: Path) -> str:
    return str(path.relative_to(workspace.root))


def _is_descendant(path: Path, parent: Path) -> bool:
    return path != parent and path.is_relative_to(parent)


def _validate_transfer_tree(workspace: Workspace, source: Path) -> None:
    allow_hidden = bool(workspace.policy.get('allow_hidden_items', False))
    stack = [source]
    while stack:
        current = stack.pop()
        if is_reparse_point(current):
            raise _violation('REPARSE_POINT_DENIED', 'Reparse point denied.', 'deny-reparse-point')
        if current != source and is_hidden(current) and not allow_hidden:
            raise _violation('HIDDEN_ITEM_DENIED', 'A hidden item is denied.', 'deny-hidden-item')
        if current.is_file():
            validate_extension(workspace, current)
        elif current.is_dir():
            stack.extend(current.iterdir())
        else:
            raise _violation('UNSUPPORTED_ITEM_TYPE', 'Unsupported item type.', 'regular-items-only')


def _validate_transfer_paths(source: Path, destination: Path) -> None:
    if source == destination:
        raise _violation(
            'SAME_SOURCE_AND_DESTINATION',
            'Source and destination must be different.',
            'distinct-transfer-paths',
        )
    if source.is_dir() and _is_descendant(destination, source):
        raise _violation(
            'DESTINATION_INSIDE_SOURCE',
            'A directory cannot be transferred into itself.',
            'deny-recursive-transfer',
        )
    if destination.is_dir() and _is_descendant(source, destination):
        raise _violation(
            'SOURCE_INSIDE_DESTINATION',
            'An ancestor of the source cannot be replaced.',
            'deny-destructive-ancestor-replacement',
        )


def _ensure_compatible_overwrite(source: Path, destination: Path) -> None:
    source_kind = item_type(source)
    destination_kind = item_type(destination)
    compatible = source_kind == destination_kind and source_kind in {
        ItemType.FILE,
        ItemType.DIRECTORY,
    }
    if not compatible:
        raise _violation(
            'INCOMPATIBLE_TARGET_TYPE',
            'Overwrite requires matching item types.',
            'compatible-overwrite-target',
        )


def _prepare_destination_parent(
    destination: Path,
    *,
    create_parents: bool,
) -> None:
    if create_parents:
        destination.parent.mkdir(parents=True, exist_ok=True)
    elif not destination.parent.is_dir():
        raise FileNotFoundError(destination.parent)


def _recycle_destination(workspace: Workspace, destination: Path) -> Path:
    _, _, recycled = move_to_recycle(
        workspace,
        _relative_to_workspace(workspace, destination),
    )
    return recycled


def _restore_recycled_destination(recycled: Path, destination: Path) -> None:
    if _path_exists(recycled) and not _path_exists(destination):
        shutil.move(str(recycled), str(destination))


def _commit_transfer(
    source: Path,
    destination: Path,
    *,
    overwrite: bool,
) -> None:
    if overwrite:
        os.replace(source, destination)
        return
    # The broker is Windows-only. On Windows, os.rename is an atomic
    # no-replace operation and raises FileExistsError if destination appears.
    os.rename(source, destination)


def move_item(
    workspace: Workspace,
    relative_path: str,
    destination_path: str,
    *,
    overwrite: bool = False,
    create_parents: bool = False,
) -> Path:
    source = resolve_workspace_path(workspace, relative_path, must_exist=True)
    destination = resolve_workspace_path(workspace, destination_path)
    _validate_transfer_tree(workspace, source)
    _validate_transfer_paths(source, destination)
    _prepare_destination_parent(destination, create_parents=create_parents)

    if _path_exists(destination):
        if not overwrite:
            raise FileExistsError(destination)
        _ensure_compatible_overwrite(source, destination)
    if source.is_file():
        validate_extension(workspace, destination)

    source = resolve_workspace_path(workspace, relative_path, must_exist=True)
    destination = resolve_workspace_path(workspace, destination_path)
    recycled = None
    if _path_exists(destination):
        if not overwrite:
            raise FileExistsError(destination)
        _ensure_compatible_overwrite(source, destination)
        recycled = _recycle_destination(workspace, destination)
    try:
        source = resolve_workspace_path(workspace, relative_path, must_exist=True)
        destination = resolve_workspace_path(workspace, destination_path)
        _validate_transfer_tree(workspace, source)
        source = resolve_workspace_path(workspace, relative_path, must_exist=True)
        destination = resolve_workspace_path(workspace, destination_path)
        _commit_transfer(
            source,
            destination,
            overwrite=overwrite,
        )
    except BaseException:
        if recycled is not None:
            _restore_recycled_destination(recycled, destination)
        raise
    return destination


def _remove_staging_path(path: Path) -> None:
    if not _path_exists(path):
        return
    if path.is_dir() and not is_reparse_point(path):
        shutil.rmtree(path)
    else:
        path.unlink()


def copy_item(
    workspace: Workspace,
    relative_path: str,
    destination_path: str,
    *,
    overwrite: bool = False,
    recursive: bool = False,
    create_parents: bool = False,
) -> Path:
    source = resolve_workspace_path(workspace, relative_path, must_exist=True)
    destination = resolve_workspace_path(workspace, destination_path)
    _validate_transfer_tree(workspace, source)
    if source.is_dir() and not recursive:
        raise _violation(
            'RECURSIVE_REQUIRED',
            'Copying a directory requires recursive=true.',
            'explicit-recursive-directory-copy',
        )
    _validate_transfer_paths(source, destination)
    _prepare_destination_parent(destination, create_parents=create_parents)

    if _path_exists(destination):
        if not overwrite:
            raise FileExistsError(destination)
        _ensure_compatible_overwrite(source, destination)
    if source.is_file():
        validate_extension(workspace, destination)

    source = resolve_workspace_path(workspace, relative_path, must_exist=True)
    destination = resolve_workspace_path(workspace, destination_path)
    _validate_transfer_tree(workspace, source)
    _validate_transfer_paths(source, destination)
    destination = resolve_workspace_path(workspace, destination_path)
    if _path_exists(destination):
        if not overwrite:
            raise FileExistsError(destination)
        _ensure_compatible_overwrite(source, destination)
    if source.is_file():
        validate_extension(workspace, destination)

    staging = destination.with_name(
        f'.{destination.name}.{uuid.uuid4().hex}.tmp'
    )
    try:
        if source.is_dir():
            shutil.copytree(source, staging, symlinks=True)
            _validate_transfer_tree(workspace, staging)
        else:
            shutil.copy2(source, staging)

        source = resolve_workspace_path(workspace, relative_path, must_exist=True)
        destination = resolve_workspace_path(workspace, destination_path)
        recycled = None
        if _path_exists(destination):
            if not overwrite:
                raise FileExistsError(destination)
            _ensure_compatible_overwrite(source, destination)
            recycled = _recycle_destination(workspace, destination)
        try:
            destination = resolve_workspace_path(workspace, destination_path)
            staging_target = staging.resolve(strict=True)
            workspace_root = workspace.root.resolve(strict=True)
            if (
                not staging_target.is_relative_to(workspace_root)
                or is_reparse_point(staging)
            ):
                raise _violation(
                    'STAGING_PATH_CHANGED',
                    'The copy staging path changed before completion.',
                    'stable-staging-path',
                )
            _commit_transfer(
                staging,
                destination,
                overwrite=overwrite,
            )
        except BaseException:
            if recycled is not None:
                _restore_recycled_destination(recycled, destination)
            raise
    finally:
        _remove_staging_path(staging)
    return destination


def move_to_recycle(
    workspace: Workspace,
    relative_path: str,
    *,
    expected_hash: str | None = None,
) -> tuple[Path, str, Path]:
    source = resolve_workspace_path(workspace, relative_path, must_exist=True)
    _validate_transfer_tree(workspace, source)
    if source.is_file():
        check_expected_version(source, expected_hash, None)
    elif expected_hash is not None:
        raise _violation(
            'FILE_TARGET_REQUIRED',
            'Hash preconditions are only valid for files.',
            'optimistic-concurrency-hash',
        )

    recycle_root = workspace.recycle_root.resolve(strict=False)
    if is_reparse_point(recycle_root):
        raise _violation(
            'REPARSE_POINT_DENIED',
            'The recycle root cannot be a reparse point.',
            'deny-reparse-point',
        )
    recycle_id = uuid.uuid4().hex
    date_part = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    relative = source.relative_to(workspace.root)
    destination = (
        recycle_root / date_part / recycle_id / relative
    ).resolve(strict=False)
    if not destination.is_relative_to(recycle_root):
        raise _violation(
            'RECYCLE_ESCAPE_DENIED',
            'The recycle destination escaped its configured root.',
            'controlled-recycle-root',
        )
    destination.parent.mkdir(parents=True, exist_ok=False)

    source = resolve_workspace_path(workspace, relative_path, must_exist=True)
    if source.is_file():
        check_expected_version(source, expected_hash, None)
    shutil.move(str(source), str(destination))
    return source, recycle_id, destination


def delete_file(
    workspace: Workspace,
    relative_path: str,
    *,
    expected_hash: str | None = None,
) -> tuple[str, Path]:
    source = resolve_workspace_path(workspace, relative_path, must_exist=True)
    _ensure_file_target(workspace, source)
    _, recycle_id, destination = move_to_recycle(
        workspace,
        relative_path,
        expected_hash=expected_hash,
    )
    return recycle_id, destination


def delete_directory(
    workspace: Workspace,
    relative_path: str,
    *,
    recursive: bool = False,
) -> tuple[str, Path]:
    source = resolve_workspace_path(workspace, relative_path, must_exist=True)
    if not source.is_dir() or is_reparse_point(source):
        raise NotADirectoryError(source)
    if not recursive and next(source.iterdir(), None) is not None:
        raise _violation(
            'RECURSIVE_REQUIRED',
            'Deleting a non-empty directory requires recursive=true.',
            'explicit-recursive-directory-delete',
        )
    _, recycle_id, destination = move_to_recycle(workspace, relative_path)
    return recycle_id, destination


def _validate_safe_regex(expression: str) -> None:
    if len(expression) > _MAXIMUM_REGEX_CHARACTERS:
        raise _violation('UNSAFE_REGEX', 'The regular expression is too long.', 'safe-regular-expression')
    unsafe_tokens = ('(?=', '(?!', '(?<=', '(?<!', '(?P=', '(?>', '(?(')
    if any(token in expression for token in unsafe_tokens):
        raise _violation('UNSAFE_REGEX', 'Lookaround and advanced groups are denied.', 'safe-regular-expression')
    if re.search(r'\(\?(?!:)', expression):
        raise _violation('UNSAFE_REGEX', 'Inline flags and advanced groups are denied.', 'safe-regular-expression')
    if any(token in expression for token in ('(', ')', '|')):
        raise _violation('UNSAFE_REGEX', 'Groups and alternation are denied.', 'safe-regular-expression')
    if re.search(r'\\[1-9]', expression):
        raise _violation('UNSAFE_REGEX', 'Backreferences are denied.', 'safe-regular-expression')
    surface = re.sub(r'\[(?:\\.|[^\]])*\]', 'C', expression)
    surface = re.sub(r'\\.', 'E', surface).replace('(?:', '(')
    depth = 0
    for character in surface:
        if character == '(':
            depth += 1
            if depth > 1:
                raise _violation('UNSAFE_REGEX', 'Nested groups are denied.', 'safe-regular-expression')
        elif character == ')':
            depth = max(0, depth - 1)
    quantifiers = re.findall(r'[*+?]|\{\d+(?:,\d*)?\}', surface)
    if len(quantifiers) > 3:
        raise _violation('UNSAFE_REGEX', 'Too many repetitions are denied.', 'safe-regular-expression')
    for bounded in re.finditer(r'\{(\d+)(?:,(\d*))?\}', surface):
        raw_upper = bounded.group(2)
        if ',' in bounded.group(0) and raw_upper == '':
            raise _violation('UNSAFE_REGEX', 'Unbounded brace repetition is denied.', 'safe-regular-expression')
        if int(raw_upper or bounded.group(1)) > 1000:
            raise _violation('UNSAFE_REGEX', 'Large repetition is denied.', 'safe-regular-expression')
    repeated: list[tuple[int, int, str]] = []
    for quantifier in re.finditer(r'(?<!\\)(?:[*+?]|\{\d+(?:,\d*)?\})', expression):
        position = quantifier.start()
        if position < 2 or expression[position - 2] != '\\':
            raise _violation('UNSAFE_REGEX', 'Only character-class repetition is allowed.', 'safe-regular-expression')
        slash_count = 0
        cursor = position - 2
        while cursor >= 0 and expression[cursor] == '\\':
            slash_count += 1
            cursor -= 1
        code = expression[position - 1]
        if slash_count % 2 == 0 or code not in 'dDsSwW':
            raise _violation('UNSAFE_REGEX', 'Only character-class repetition is allowed.', 'safe-regular-expression')
        repeated.append((position - 2, quantifier.end(), code))
    categories = {'d': {'digit'}, 's': {'space'}, 'w': {'digit', 'word'}}
    for previous, current in zip(repeated, repeated[1:]):
        if previous[1] != current[0]:
            continue
        left = categories.get(previous[2])
        right = categories.get(current[2])
        if left is None or right is None or not left.isdisjoint(right):
            raise _violation('UNSAFE_REGEX', 'Adjacent repetitions overlap.', 'safe-regular-expression')
    if re.search(r'\([^()]*(?:\*|\+|\{\d+,?\d*\})[^()]*\)\s*(?:\*|\+|\{)', expression):
        raise _violation('UNSAFE_REGEX', 'Nested repetition is denied.', 'safe-regular-expression')
    if re.search(r'\([^()]*\|[^()]*\)\s*(?:\*|\+|\{)', expression):
        raise _violation('UNSAFE_REGEX', 'Repeated alternation is denied.', 'safe-regular-expression')
    if expression.count('.*') > 1:
        raise _violation('UNSAFE_REGEX', 'Repeated wildcards are denied.', 'safe-regular-expression')


def _escape_literal(search_text: str) -> str:
    # A literal search must not depend on the line ending a file happens to
    # use. Callers send "\n" (JSON has no other option) while Windows files
    # are usually CRLF, so every literal newline matches either form.
    segments = _normalize_newlines(search_text).split('\n')
    return r'\r?\n'.join(re.escape(segment) for segment in segments)


def _normalize_newlines(text: str) -> str:
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _align_newlines(text: str, use_crlf: bool) -> str:
    # Replacement text inherits the line ending of the file being edited so a
    # CRLF file never ends up with mixed endings.
    normalized = _normalize_newlines(text)
    return normalized.replace('\n', '\r\n') if use_crlf else normalized


def _compile_pattern(
    search_text: str,
    *,
    case_sensitive: bool,
    use_regex: bool,
    whole_word: bool,
) -> re.Pattern[str]:
    if not search_text:
        raise _violation('EMPTY_SEARCH_TEXT', 'Search text cannot be empty.', 'non-empty-search-text')
    if len(search_text) > _MAXIMUM_REGEX_CHARACTERS:
        raise _violation(
            'SEARCH_TEXT_LIMIT_EXCEEDED',
            'Search text exceeds the configured safe limit.',
            'bounded-search-text',
        )
    expression = search_text if use_regex else _escape_literal(search_text)
    flags = 0 if case_sensitive else re.IGNORECASE
    if use_regex:
        try:
            re.compile(expression, flags)
        except re.error as exc:
            raise _violation('INVALID_REGEX', 'The regular expression is invalid.', 'safe-regular-expression') from exc
        _validate_safe_regex(expression)
    if whole_word:
        expression = rf'\b(?:{expression})\b'
    try:
        return re.compile(expression, flags)
    except re.error as exc:
        raise _violation('INVALID_REGEX', 'The regular expression is invalid.', 'safe-regular-expression') from exc


def _search_limits(
    workspace: Workspace,
    max_depth: int,
    max_results: int,
    skip: int,
) -> tuple[int, int, int]:
    if max_depth < 0 or max_results < 1 or skip < 0:
        raise _violation('INVALID_LIMIT', 'Search limits are invalid.', 'bounded-search')
    depth_cap = _policy_int(
        workspace,
        'maximum_search_depth',
        _DEFAULT_MAXIMUM_SEARCH_DEPTH,
    )
    result_cap = _policy_int(
        workspace,
        'maximum_search_results',
        _DEFAULT_MAXIMUM_SEARCH_RESULTS,
    )
    scan_cap = _policy_int(
        workspace,
        'maximum_search_entries',
        _DEFAULT_MAXIMUM_SEARCH_ENTRIES,
    )
    return min(max_depth, depth_cap), min(max_results, result_cap), scan_cap


def _iter_search_candidates(
    workspace: Workspace,
    root: Path,
    *,
    recursive: bool,
    max_depth: int,
    include_files: bool,
    include_directories: bool,
    include_hidden: bool,
    scan_cap: int,
) -> Iterator[Path]:
    allow_hidden = bool(workspace.policy.get('allow_hidden_items', False))
    show_hidden = include_hidden and allow_hidden
    scanned = 0
    def visit(current: Path, depth: int) -> Iterator[Path]:
        nonlocal scanned
        if is_reparse_point(current) or not current.is_dir():
            raise _violation(
                'REPARSE_POINT_DENIED',
                'A search directory changed during traversal.',
                'deny-reparse-point',
            )
        entries: list[Path] = []
        for entry in current.iterdir():
            scanned += 1
            if scanned > scan_cap:
                raise _violation(
                    'SEARCH_SCAN_LIMIT_EXCEEDED',
                    'The search inspected too many filesystem entries.',
                    'maximum-search-entries',
                )
            entries.append(entry)
        entries.sort(key=lambda path: path.name.casefold())
        if is_reparse_point(current):
            raise _violation('REPARSE_POINT_DENIED', 'A search directory changed.', 'deny-reparse-point')
        for discovered in entries:
            if is_hidden(discovered) and not show_hidden:
                continue
            try:
                entry = resolve_workspace_path(
                    workspace,
                    str(discovered.relative_to(workspace.root)),
                    must_exist=True,
                )
            except FileNotFoundError:
                continue
            if is_reparse_point(entry):
                continue
            if is_hidden(entry) and not show_hidden:
                continue
            if entry.is_dir():
                if include_directories:
                    yield entry
                if recursive and depth < max_depth:
                    yield from visit(entry, depth + 1)
            elif entry.is_file() and include_files:
                yield entry
    yield from visit(root, 0)


def _extension_is_allowed(workspace: Workspace, path: Path) -> bool:
    allowed = {
        str(value).casefold()
        for value in workspace.policy.get('allowed_extensions', [])
    }
    return path.suffix.casefold() in allowed


def _glob_matches(name: str, pattern: str | None) -> bool:
    return pattern is None or fnmatch.fnmatchcase(
        name.casefold(),
        pattern.casefold(),
    )


def search_files(
    workspace: Workspace,
    relative_path: str,
    *,
    recursive: bool,
    max_depth: int,
    search_pattern: str | None,
    name_pattern: str | None,
    extension: str | None,
    include_files: bool,
    include_directories: bool,
    max_results: int,
    skip: int,
    include_hidden: bool = False,
    return_truncated: bool = False,
) -> (
    tuple[list[FileSystemItem], int]
    | tuple[list[FileSystemItem], int, bool]
):
    root = resolve_workspace_path(
        workspace,
        relative_path,
        must_exist=True,
        allow_root=True,
    )
    if not root.is_dir() or is_reparse_point(root):
        raise NotADirectoryError(root)
    depth, page_limit, scan_cap = _search_limits(
        workspace,
        max_depth,
        max_results,
        skip,
    )
    normalized_extension = None
    if extension:
        normalized_extension = extension.casefold()
        if not normalized_extension.startswith('.'):
            normalized_extension = f'.{normalized_extension}'

    filtered: list[Path] = []
    for candidate in _iter_search_candidates(
        workspace,
        root,
        recursive=recursive,
        max_depth=depth,
        include_files=include_files,
        include_directories=include_directories,
        include_hidden=include_hidden,
        scan_cap=scan_cap,
    ):
        if not _glob_matches(candidate.name, search_pattern):
            continue
        if not _glob_matches(candidate.name, name_pattern):
            continue
        if candidate.is_file():
            if not _extension_is_allowed(workspace, candidate):
                continue
            if (
                normalized_extension
                and candidate.suffix.casefold() != normalized_extension
            ):
                continue
        elif normalized_extension:
            continue
        filtered.append(candidate)

    filtered.sort(
        key=lambda path: str(path.relative_to(workspace.root)).casefold()
    )
    result_cap = _policy_int(
        workspace,
        'maximum_search_results',
        _DEFAULT_MAXIMUM_SEARCH_RESULTS,
    )
    capped = filtered[:result_cap]
    truncated = len(filtered) > result_cap
    total = len(capped)
    page = capped[skip : skip + page_limit]
    items: list[FileSystemItem] = []
    for path in page:
        try:
            items.append(make_item(workspace, path))
        except FileNotFoundError:
            continue
    if return_truncated:
        return items, total, truncated
    return items, total


def _read_searchable_text(
    workspace: Workspace,
    path: Path,
) -> str | None:
    maximum = _policy_int(
        workspace,
        'maximum_file_size_bytes',
        _DEFAULT_MAXIMUM_FILE_SIZE,
    )
    try:
        if is_reparse_point(path):
            return None
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            current = os.stat(path, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                return None
            if opened.st_size > maximum:
                return None
            data = stream.read(maximum + 1)
    except (FileNotFoundError, OSError):
        return None
    if len(data) > maximum:
        return None
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        # The broker can write UTF-16 files, so it must be able to search them.
        try:
            return data.decode('utf-16')
        except (UnicodeDecodeError, UnicodeError):
            return None
    if b'\x00' in data:
        return None
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return None


def _line_start_offsets(text: str) -> list[int]:
    """Return the offset at which every line of `text` begins."""

    offsets = [0]
    start = text.find('\n')
    while start != -1:
        offsets.append(start + 1)
        start = text.find('\n', start + 1)
    return offsets


def _locate_offset(offsets: list[int], offset: int) -> tuple[int, int]:
    """Map an absolute offset to a 1-based (line number, column number)."""

    index = bisect_right(offsets, offset) - 1
    return index + 1, offset - offsets[index] + 1


def _line_text(text: str, offsets: list[int], line_index: int) -> str:
    start = offsets[line_index - 1]
    end = offsets[line_index] - 1 if line_index < len(offsets) else len(text)
    return text[start:end].rstrip('\r')


def search_content(
    workspace: Workspace,
    relative_path: str,
    search_text: str,
    *,
    recursive: bool,
    max_depth: int,
    search_pattern: str | None,
    extension: str | None,
    case_sensitive: bool,
    use_regex: bool,
    whole_word: bool,
    max_results: int,
    skip: int,
    include_hidden: bool = False,
    return_truncated: bool = False,
) -> (
    tuple[list[ContentMatch], int]
    | tuple[list[ContentMatch], int, bool]
):
    pattern = _compile_pattern(
        search_text,
        case_sensitive=case_sensitive,
        use_regex=use_regex,
        whole_word=whole_word,
    )
    root = resolve_workspace_path(
        workspace,
        relative_path,
        must_exist=True,
        allow_root=True,
    )
    if not root.is_dir() or is_reparse_point(root):
        raise NotADirectoryError(root)
    depth, page_limit, scan_cap = _search_limits(
        workspace,
        max_depth,
        max_results,
        skip,
    )
    result_cap = _policy_int(
        workspace,
        'maximum_search_results',
        _DEFAULT_MAXIMUM_SEARCH_RESULTS,
    )
    normalized_extension = None
    if extension:
        normalized_extension = extension.casefold()
        if not normalized_extension.startswith('.'):
            normalized_extension = f'.{normalized_extension}'

    matches: list[ContentMatch] = []
    candidates = _iter_search_candidates(
        workspace,
        root,
        recursive=recursive,
        max_depth=depth,
        include_files=True,
        include_directories=False,
        include_hidden=include_hidden,
        scan_cap=scan_cap,
    )
    truncated = False
    for path in candidates:
        if truncated:
            break
        if not _glob_matches(path.name, search_pattern):
            continue
        if not _extension_is_allowed(workspace, path):
            continue
        if normalized_extension and path.suffix.casefold() != normalized_extension:
            continue
        text = _read_searchable_text(workspace, path)
        if text is None:
            continue
        relative = str(path.relative_to(workspace.root))
        # The whole file is searched, not each line in isolation, so a
        # multi-line search text behaves exactly as it does in REPLACE_TEXT.
        offsets = _line_start_offsets(text)
        for found in pattern.finditer(text):
            line_number, column_number = _locate_offset(offsets, found.start())
            matches.append(
                ContentMatch(
                    relativePath=relative,
                    lineNumber=line_number,
                    columnNumber=column_number,
                    matchedText=found.group(0)[:_MAXIMUM_MATCH_TEXT_CHARACTERS],
                    lineText=_line_text(text, offsets, line_number)[
                        :_MAXIMUM_MATCH_TEXT_CHARACTERS
                    ],
                )
            )
            if len(matches) >= result_cap:
                truncated = True
                break
    total = len(matches)
    page = matches[skip : skip + page_limit]
    if return_truncated:
        return page, total, truncated
    return page, total
