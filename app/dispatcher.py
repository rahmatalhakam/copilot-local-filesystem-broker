from __future__ import annotations

import time
import uuid
from typing import Any

from app.config import AppConfig, Workspace
from app.errors import (
    PolicyViolation,
    RequestValidationError as OperationValidationError,
)
from app.filesystem import (
    append_file,
    copy_item,
    create_directory,
    create_file,
    delete_directory,
    delete_file,
    get_metadata,
    list_directory,
    make_item,
    move_item,
    path_exists,
    read_file,
    replace_text,
    search_content,
    search_files,
    update_file,
)
from app.models import (
    FileOperationRequest,
    FileOperationResponse,
    ItemType,
    Operation,
    Status,
)
from app.powershell import execute_restricted_command


_PERMISSION_BY_OPERATION = {
    Operation.CREATE_FILE: "create",
    Operation.CREATE_DIRECTORY: "create",
    Operation.READ_FILE: "read",
    Operation.LIST_DIRECTORY: "read",
    Operation.UPDATE_FILE: "update",
    Operation.APPEND_FILE: "update",
    Operation.REPLACE_TEXT: "update",
    Operation.DELETE_FILE: "delete",
    Operation.DELETE_DIRECTORY: "delete",
    Operation.MOVE: "move",
    Operation.COPY: "copy",
    Operation.SEARCH_FILES: "search",
    Operation.SEARCH_CONTENT: "search",
    Operation.GET_METADATA: "read",
    Operation.EXISTS: "read",
    Operation.EXECUTE_COMMAND: "execute_command",
}

_REQUIRED_FIELDS: dict[Operation, tuple[str, ...]] = {
    Operation.CREATE_FILE: ("path", "content"),
    Operation.CREATE_DIRECTORY: ("path",),
    Operation.READ_FILE: ("path",),
    Operation.LIST_DIRECTORY: ("path",),
    Operation.UPDATE_FILE: ("path", "content"),
    Operation.APPEND_FILE: ("path", "content"),
    Operation.REPLACE_TEXT: (
        "path",
        "searchText",
        "replacementText",
    ),
    Operation.DELETE_FILE: ("path",),
    Operation.DELETE_DIRECTORY: ("path",),
    Operation.MOVE: ("path", "destinationPath"),
    Operation.COPY: ("path", "destinationPath"),
    Operation.SEARCH_FILES: ("path",),
    Operation.SEARCH_CONTENT: ("path", "searchText"),
    Operation.GET_METADATA: ("path",),
    Operation.EXISTS: ("path",),
    Operation.EXECUTE_COMMAND: ("shellCommand",),
}


def _require_fields(request: FileOperationRequest) -> None:
    for field_name in _REQUIRED_FIELDS[request.operation]:
        if getattr(request, field_name) is None:
            raise OperationValidationError(
                f"Field '{field_name}' is required for "
                f"{request.operation.value}."
            )


def _reject_value_unless(
    request: FileOperationRequest,
    field_name: str,
    allowed_operations: set[Operation],
    *,
    active: bool,
) -> None:
    if active and request.operation not in allowed_operations:
        raise OperationValidationError(
            f"Field '{field_name}' is not valid for "
            f"{request.operation.value}."
        )


def validate_operation_request(request: FileOperationRequest) -> None:
    """Validate the operation-specific portion of the fixed superset schema."""

    _require_fields(request)

    _reject_value_unless(
        request,
        "destinationPath",
        {Operation.MOVE, Operation.COPY},
        active=request.destinationPath is not None,
    )
    _reject_value_unless(
        request,
        "content",
        {
            Operation.CREATE_FILE,
            Operation.UPDATE_FILE,
            Operation.APPEND_FILE,
        },
        active=request.content is not None,
    )
    _reject_value_unless(
        request,
        "searchText",
        {Operation.REPLACE_TEXT, Operation.SEARCH_CONTENT},
        active=request.searchText is not None,
    )
    _reject_value_unless(
        request,
        "replacementText",
        {Operation.REPLACE_TEXT},
        active=request.replacementText is not None,
    )
    _reject_value_unless(
        request,
        "expectedHash",
        {
            Operation.UPDATE_FILE,
            Operation.APPEND_FILE,
            Operation.REPLACE_TEXT,
            Operation.DELETE_FILE,
        },
        active=request.expectedHash is not None,
    )
    _reject_value_unless(
        request,
        "expectedLastModifiedUtc",
        {Operation.UPDATE_FILE},
        active=request.expectedLastModifiedUtc is not None,
    )
    _reject_value_unless(
        request,
        "searchPattern",
        {Operation.SEARCH_FILES, Operation.SEARCH_CONTENT},
        active=request.searchPattern is not None,
    )
    _reject_value_unless(
        request,
        "namePattern",
        {Operation.SEARCH_FILES},
        active=request.namePattern is not None,
    )
    _reject_value_unless(
        request,
        "fileExtension",
        {Operation.SEARCH_FILES, Operation.SEARCH_CONTENT},
        active=request.fileExtension is not None,
    )
    _reject_value_unless(
        request,
        "useRegex",
        {Operation.REPLACE_TEXT, Operation.SEARCH_CONTENT},
        active=request.useRegex,
    )
    _reject_value_unless(
        request,
        "wholeWord",
        {Operation.REPLACE_TEXT, Operation.SEARCH_CONTENT},
        active=request.wholeWord,
    )
    _reject_value_unless(
        request,
        "caseSensitive",
        {Operation.REPLACE_TEXT, Operation.SEARCH_CONTENT},
        active=request.caseSensitive,
    )
    _reject_value_unless(
        request,
        "replaceAll",
        {Operation.REPLACE_TEXT},
        active=request.replaceAll,
    )
    _reject_value_unless(
        request,
        "recursive",
        {
            Operation.LIST_DIRECTORY,
            Operation.DELETE_DIRECTORY,
            Operation.COPY,
            Operation.SEARCH_FILES,
            Operation.SEARCH_CONTENT,
        },
        active=request.recursive,
    )
    _reject_value_unless(
        request,
        "overwrite",
        {
            Operation.CREATE_FILE,
            Operation.CREATE_DIRECTORY,
            Operation.MOVE,
            Operation.COPY,
        },
        active=request.overwrite,
    )
    _reject_value_unless(
        request,
        "createParentDirectories",
        {
            Operation.CREATE_FILE,
            Operation.CREATE_DIRECTORY,
            Operation.MOVE,
            Operation.COPY,
        },
        active=request.createParentDirectories,
    )

    if request.force:
        raise OperationValidationError(
            "Field 'force' is reserved and is not enabled in the MVP."
        )

    if request.operation == Operation.EXECUTE_COMMAND:
        command_incompatible = (
            request.destinationPath is not None
            or request.content is not None
            or request.searchText is not None
            or request.replacementText is not None
            or request.expectedHash is not None
            or request.expectedLastModifiedUtc is not None
        )
        if command_incompatible:
            raise OperationValidationError(
                "EXECUTE_COMMAND cannot include filesystem mutation fields."
            )
    elif request.shellCommand is not None or request.shellArguments:
        raise OperationValidationError(
            "PowerShell fields are valid only for EXECUTE_COMMAND."
        )


def _workspace_for(
    app_config: AppConfig,
    request: FileOperationRequest,
) -> tuple[Workspace, str]:
    workspace = app_config.workspaces.get(request.workspace)
    if workspace is None:
        raise PolicyViolation(
            "WORKSPACE_NOT_FOUND",
            "The requested workspace alias is not configured.",
            "configured-workspaces-only",
        )

    permission = _PERMISSION_BY_OPERATION[request.operation]
    if not workspace.permissions.get(permission, False):
        raise PolicyViolation(
            "OPERATION_DENIED",
            "The selected workspace does not permit this operation.",
            f"workspace-permission-{permission}",
        )
    return workspace, permission


def _metadata_values(
    item: Any,
    *,
    include: bool,
) -> dict[str, Any]:
    if not include:
        # Hash selection is independent from the remaining metadata fields.
        # make_item computes it only when returnHash was requested.
        return {'hash': item.hash} if item.hash is not None else {}
    return {
        "name": item.name,
        "extension": item.extension,
        "sizeBytes": item.sizeBytes,
        "createdUtc": item.createdUtc,
        "modifiedUtc": item.modifiedUtc,
        "hash": item.hash,
    }


def _pagination_values(
    *,
    returned: int,
    total: int,
    skip: int,
    truncated: bool = False,
) -> dict[str, Any]:
    # `truncated` marks a result set clipped by maximum_search_results: the
    # caller must be told more matches exist even though they are not pageable.
    has_more = skip + returned < total
    return {
        "affectedCount": returned,
        "totalResults": total,
        "returnedResults": returned,
        "hasMore": has_more or truncated,
        "nextSkip": skip + returned if (has_more or truncated) else None,
    }


def dispatch(
    app_config: AppConfig,
    request: FileOperationRequest,
) -> FileOperationResponse:
    started = time.perf_counter()
    validate_operation_request(request)
    workspace, permission = _workspace_for(app_config, request)

    base: dict[str, Any] = {
        "success": True,
        "status": Status.COMPLETED,
        "operation": request.operation.value,
        "operationId": str(uuid.uuid4()),
        "correlationId": request.correlationId,
        "workspace": request.workspace,
        "path": request.path,
        "destinationPath": request.destinationPath,
        "message": "Operation completed.",
        "policyAllowed": True,
        "policyRule": f"workspace-{permission}",
    }

    def response(message: str, **values: Any) -> FileOperationResponse:
        payload = {**base, "message": message, **values}
        result = FileOperationResponse(**payload)
        result.durationMs = int((time.perf_counter() - started) * 1000)
        return result

    operation = request.operation

    if operation == Operation.CREATE_FILE:
        target = create_file(
            workspace,
            request.path,
            request.content,
            request.encoding,
            overwrite=request.overwrite,
            create_parents=request.createParentDirectories,
        )
        item = make_item(workspace, target, include_hash=request.returnHash)
        return response(
            "File created successfully.",
            exists=True,
            itemType=ItemType.FILE,
            affectedCount=1,
            **_metadata_values(item, include=request.returnMetadata),
        )

    if operation == Operation.CREATE_DIRECTORY:
        target = create_directory(
            workspace,
            request.path,
            create_parents=request.createParentDirectories,
            overwrite=request.overwrite,
        )
        item = make_item(workspace, target)
        return response(
            "Directory created successfully.",
            exists=True,
            itemType=ItemType.DIRECTORY,
            affectedCount=1,
            **_metadata_values(item, include=request.returnMetadata),
        )

    if operation == Operation.READ_FILE:
        target, content, truncated = read_file(
            workspace,
            request.path,
            request.encoding,
            max_characters=request.maxContentCharacters,
        )
        item = make_item(workspace, target, include_hash=request.returnHash)
        return response(
            (
                "File read successfully; returned content was truncated."
                if truncated
                else "File read successfully."
            ),
            status=Status.PARTIAL if truncated else Status.COMPLETED,
            exists=True,
            itemType=ItemType.FILE,
            content=content if request.returnContent else None,
            encoding=request.encoding,
            contentTruncated=truncated,
            affectedCount=1,
            totalResults=1,
            returnedResults=1,
            **_metadata_values(item, include=request.returnMetadata),
        )

    if operation == Operation.LIST_DIRECTORY:
        items, total = list_directory(
            workspace,
            request.path,
            recursive=request.recursive,
            include_files=request.includeFiles,
            include_directories=request.includeDirectories,
            include_hidden=request.includeHidden,
            max_depth=request.maxDepth,
            max_results=request.maxResults,
            skip=request.skip,
        )
        return response(
            "Directory listed successfully.",
            exists=True,
            itemType=ItemType.DIRECTORY,
            items=items,
            **_pagination_values(
                returned=len(items),
                total=total,
                skip=request.skip,
            ),
        )

    if operation == Operation.UPDATE_FILE:
        target = update_file(
            workspace,
            request.path,
            request.content,
            request.encoding,
            expected_hash=request.expectedHash,
            expected_modified=request.expectedLastModifiedUtc,
        )
        item = make_item(workspace, target, include_hash=request.returnHash)
        return response(
            "File updated successfully.",
            exists=True,
            itemType=ItemType.FILE,
            affectedCount=1,
            **_metadata_values(item, include=request.returnMetadata),
        )

    if operation == Operation.APPEND_FILE:
        target = append_file(
            workspace,
            request.path,
            request.content,
            request.encoding,
            append_newline=request.appendNewLine,
            expected_hash=request.expectedHash,
        )
        item = make_item(workspace, target, include_hash=request.returnHash)
        return response(
            "Content appended successfully.",
            exists=True,
            itemType=ItemType.FILE,
            affectedCount=1,
            **_metadata_values(item, include=request.returnMetadata),
        )

    if operation == Operation.REPLACE_TEXT:
        target, replacements = replace_text(
            workspace,
            request.path,
            request.searchText,
            request.replacementText,
            encoding=request.encoding,
            case_sensitive=request.caseSensitive,
            use_regex=request.useRegex,
            whole_word=request.wholeWord,
            expected_occurrences=(
                request.expectedOccurrences
                if "expectedOccurrences" in request.model_fields_set
                else None
            ),
            replace_all=request.replaceAll,
            expected_hash=request.expectedHash,
        )
        item = make_item(workspace, target, include_hash=request.returnHash)
        return response(
            f"Replaced {replacements} occurrence(s).",
            exists=True,
            itemType=ItemType.FILE,
            affectedCount=replacements,
            **_metadata_values(item, include=request.returnMetadata),
        )

    if operation == Operation.DELETE_FILE:
        recycle_id, destination = delete_file(
            workspace,
            request.path,
            expected_hash=request.expectedHash,
        )
        return response(
            "File moved to the recycle directory.",
            exists=False,
            itemType=ItemType.FILE,
            affectedCount=1,
            recycleId=recycle_id,
            recyclePath=str(destination),
        )

    if operation == Operation.DELETE_DIRECTORY:
        recycle_id, destination = delete_directory(
            workspace,
            request.path,
            recursive=request.recursive,
        )
        return response(
            "Directory moved to the recycle directory.",
            exists=False,
            itemType=ItemType.DIRECTORY,
            affectedCount=1,
            recycleId=recycle_id,
            recyclePath=str(destination),
        )

    if operation == Operation.MOVE:
        target = move_item(
            workspace,
            request.path,
            request.destinationPath,
            overwrite=request.overwrite,
            create_parents=request.createParentDirectories,
        )
        item = make_item(workspace, target, include_hash=request.returnHash)
        return response(
            "Item moved successfully.",
            exists=True,
            itemType=item.itemType,
            affectedCount=1,
            **_metadata_values(item, include=request.returnMetadata),
        )

    if operation == Operation.COPY:
        target = copy_item(
            workspace,
            request.path,
            request.destinationPath,
            overwrite=request.overwrite,
            recursive=request.recursive,
            create_parents=request.createParentDirectories,
        )
        item = make_item(workspace, target, include_hash=request.returnHash)
        return response(
            "Item copied successfully.",
            exists=True,
            itemType=item.itemType,
            affectedCount=1,
            **_metadata_values(item, include=request.returnMetadata),
        )

    if operation == Operation.SEARCH_FILES:
        items, total, truncated = search_files(
            workspace,
            request.path,
            recursive=request.recursive,
            max_depth=request.maxDepth,
            search_pattern=request.searchPattern,
            name_pattern=request.namePattern,
            extension=request.fileExtension,
            include_files=request.includeFiles,
            include_directories=request.includeDirectories,
            include_hidden=request.includeHidden,
            max_results=request.maxResults,
            skip=request.skip,
            return_truncated=True,
        )
        return response(
            (
                "File search completed; the result limit was reached."
                if truncated
                else "File search completed."
            ),
            status=Status.PARTIAL if truncated else Status.COMPLETED,
            items=items,
            **_pagination_values(
                returned=len(items),
                total=total,
                skip=request.skip,
                truncated=truncated,
            ),
        )

    if operation == Operation.SEARCH_CONTENT:
        matches, total, truncated = search_content(
            workspace,
            request.path,
            request.searchText,
            recursive=request.recursive,
            max_depth=request.maxDepth,
            search_pattern=request.searchPattern,
            extension=request.fileExtension,
            case_sensitive=request.caseSensitive,
            use_regex=request.useRegex,
            whole_word=request.wholeWord,
            max_results=request.maxResults,
            skip=request.skip,
            include_hidden=request.includeHidden,
            return_truncated=True,
        )
        return response(
            (
                "Content search completed; the result limit was reached."
                if truncated
                else "Content search completed."
            ),
            status=Status.PARTIAL if truncated else Status.COMPLETED,
            matches=matches,
            **_pagination_values(
                returned=len(matches),
                total=total,
                skip=request.skip,
                truncated=truncated,
            ),
        )

    if operation == Operation.GET_METADATA:
        item = get_metadata(
            workspace,
            request.path,
            include_hash=request.returnHash,
        )
        return response(
            "Metadata read successfully.",
            exists=True,
            itemType=item.itemType,
            affectedCount=1,
            totalResults=1,
            returnedResults=1,
            **_metadata_values(item, include=request.returnMetadata),
        )

    if operation == Operation.EXISTS:
        exists, target_type = path_exists(workspace, request.path)
        return response(
            "Existence check completed.",
            exists=exists,
            itemType=target_type,
            affectedCount=1 if exists else 0,
            totalResults=1 if exists else 0,
            returnedResults=1 if exists else 0,
        )

    command = request.shellCommand
    exit_code, stdout, stderr, truncated = execute_restricted_command(
        app_config,
        workspace,
        command,
        request.shellArguments,
        request.timeoutSeconds,
    )
    succeeded = exit_code == 0
    status = (
        Status.PARTIAL
        if succeeded and truncated
        else Status.COMPLETED
        if succeeded
        else Status.FAILED
    )
    return response(
        (
            "Command completed; returned output was truncated."
            if succeeded and truncated
            else "Command completed."
            if succeeded
            else "Command returned a non-zero exit code."
        ),
        success=succeeded,
        status=status,
        exitCode=exit_code,
        stdout=stdout,
        stderr=stderr,
        outputTruncated=truncated,
    )


__all__ = [
    "OperationValidationError",
    "dispatch",
    "validate_operation_request",
]
