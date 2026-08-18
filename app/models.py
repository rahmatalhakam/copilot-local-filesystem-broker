from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class Operation(StrEnum):
    CREATE_FILE = "CREATE_FILE"
    CREATE_DIRECTORY = "CREATE_DIRECTORY"
    READ_FILE = "READ_FILE"
    LIST_DIRECTORY = "LIST_DIRECTORY"
    UPDATE_FILE = "UPDATE_FILE"
    APPEND_FILE = "APPEND_FILE"
    REPLACE_TEXT = "REPLACE_TEXT"
    DELETE_FILE = "DELETE_FILE"
    DELETE_DIRECTORY = "DELETE_DIRECTORY"
    MOVE = "MOVE"
    COPY = "COPY"
    SEARCH_FILES = "SEARCH_FILES"
    SEARCH_CONTENT = "SEARCH_CONTENT"
    GET_METADATA = "GET_METADATA"
    EXISTS = "EXISTS"
    EXECUTE_COMMAND = "EXECUTE_COMMAND"


class Status(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CONFLICT = "CONFLICT"
    NOT_FOUND = "NOT_FOUND"


class ItemType(StrEnum):
    FILE = "FILE"
    DIRECTORY = "DIRECTORY"
    REPARSE_POINT = "REPARSE_POINT"
    OTHER = "OTHER"


class FileOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Operation
    workspace: str = Field(min_length=1, max_length=100)

    path: str | None = Field(default=None, max_length=1000)
    destinationPath: str | None = Field(default=None, max_length=1000)

    content: str | None = Field(default=None, max_length=1000000)
    encoding: str = Field(
        default="utf-8",
        pattern=r"^(utf-8|utf-8-bom|ascii|unicode|base64)$",
    )

    overwrite: bool = False
    appendNewLine: bool = True
    createParentDirectories: bool = False

    recursive: bool = False
    force: bool = False

    searchPattern: str | None = Field(default=None, max_length=200)
    searchText: str | None = Field(default=None, max_length=10000)
    replacementText: str | None = Field(default=None, max_length=1000000)
    expectedOccurrences: int | None = Field(default=None, ge=1, le=10000)
    replaceAll: bool = False
    caseSensitive: bool = True
    useRegex: bool = False
    wholeWord: bool = False

    includeFiles: bool = True
    includeDirectories: bool = False
    includeHidden: bool = False
    fileExtension: str | None = Field(default=None, max_length=50)
    namePattern: str | None = Field(default=None, max_length=200)

    maxDepth: int = Field(default=10, ge=0, le=20)
    maxResults: int = Field(default=100, ge=1, le=1000)
    skip: int = Field(default=0, ge=0)

    maxContentCharacters: int = Field(default=100000, ge=0, le=400000)
    returnContent: bool = True
    returnMetadata: bool = True
    returnHash: bool = False

    expectedHash: str | None = Field(
        default=None,
        min_length=71,
        max_length=71,
        pattern=r'^sha256:[0-9A-Fa-f]{64}$',
    )
    expectedLastModifiedUtc: datetime | None = None

    shellCommand: str | None = Field(default=None, max_length=100)
    shellArguments: list[Annotated[str, Field(max_length=1000)]] = Field(
        default_factory=list,
        max_length=50,
    )

    reason: str | None = Field(default=None, max_length=1000)
    timeoutSeconds: int = Field(default=20, ge=1, le=60)
    correlationId: str | None = Field(default=None, max_length=200)


class FileSystemItem(BaseModel):
    name: str
    relativePath: str
    itemType: ItemType
    extension: str | None = None
    sizeBytes: int | None = None
    createdUtc: datetime | None = None
    modifiedUtc: datetime | None = None
    isHidden: bool = False
    isReadOnly: bool = False
    hash: str | None = None


class ContentMatch(BaseModel):
    relativePath: str
    lineNumber: int
    columnNumber: int
    matchedText: str
    lineText: str
    beforeText: str | None = None
    afterText: str | None = None


class FileOperationResponse(BaseModel):
    success: bool
    status: Status
    operation: str
    operationId: str
    correlationId: str | None = None

    workspace: str | None = None
    path: str | None = None
    destinationPath: str | None = None

    message: str
    errorCode: str | None = None
    errorMessage: str | None = None

    exists: bool | None = None
    itemType: ItemType | None = None

    content: str | None = None
    encoding: str | None = None
    contentTruncated: bool = False

    name: str | None = None
    extension: str | None = None
    sizeBytes: int | None = None
    createdUtc: datetime | None = None
    modifiedUtc: datetime | None = None
    hash: str | None = None

    affectedCount: int = 0
    totalResults: int = 0
    returnedResults: int = 0
    hasMore: bool = False
    nextSkip: int | None = None

    items: list[FileSystemItem] = Field(default_factory=list)
    matches: list[ContentMatch] = Field(default_factory=list)

    exitCode: int | None = None
    stdout: str = ""
    stderr: str = ""
    outputTruncated: bool = False

    recycleId: str | None = None
    recyclePath: str | None = None

    durationMs: int = 0

    policyAllowed: bool = True
    policyRule: str | None = None
