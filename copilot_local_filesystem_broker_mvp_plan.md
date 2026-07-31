# Copilot Local Filesystem Broker — MVP Implementation Plan

**Target platform:** Windows  
**Server:** Python 3.12+ with FastAPI  
**Shell:** PowerShell 7 (`pwsh.exe`)  
**Client path:** Microsoft Copilot agent → Power Platform custom connector → on-premises data gateway → local FastAPI server  
**API contract:** Swagger / OpenAPI 2.0  
**Authentication:** None for this local proof of concept  
**Workspace model:** Multiple configured aliases; initial alias `demo` maps to `D:\CopilotPOC\Workspace`  
**Deletion:** Move items into a controlled recycle directory  
**Execution modes:** Structured filesystem operations plus restricted PowerShell command-and-argument execution  
**Explicitly excluded:** Arbitrary PowerShell script text

---

## 1. Purpose

This proof of concept demonstrates that a Microsoft Copilot agent can create, read, update, delete, list, move, copy, search, and inspect files on a Windows machine through one Power Platform custom connector operation.

The design intentionally exposes a single synchronous JSON endpoint:

```http
POST /api/v1/filesystem/execute
```

The endpoint accepts a fixed superset request schema. The `operation` field determines which fields are relevant. Every request property is declared in Swagger 2.0 so that Power Apps, Power Automate, and Copilot Studio can understand the connector inputs.

The server supports two modes:

1. **Structured mode:** The caller selects a known filesystem operation such as `READ_FILE` or `SEARCH_CONTENT`.
2. **Restricted command mode:** The caller selects `EXECUTE_COMMAND`, supplies one allowlisted PowerShell command, and supplies arguments as an explicit array.

The server never accepts arbitrary PowerShell script text.

---

## 2. Confirmed scope

### 2.1 Included operations

- Create file
- Create directory
- Read file
- List directory
- Update complete file
- Append content
- Replace text
- Delete file by moving it into the recycle directory
- Delete directory by moving it into the recycle directory
- Move
- Copy
- Search filenames and directory names
- Search file contents
- Read metadata
- Check existence
- Execute a restricted allowlisted PowerShell command

### 2.2 Excluded from the MVP

- Arbitrary PowerShell scripts
- Command chaining
- Nested shells
- Authentication
- Access to arbitrary absolute paths
- Access outside configured workspaces
- Direct permanent deletion
- Symbolic-link or Windows reparse-point traversal
- Streaming or Server-Sent Events
- Long-running asynchronous jobs
- Network share access unless a workspace is explicitly configured for one later
- Running application code, installers, package managers, or downloaded executables

---

## 3. Architecture

```text
┌───────────────────────────────┐
│ Microsoft Copilot agent       │
│ Copilot Studio action/tool    │
└───────────────┬───────────────┘
                │ synchronous JSON
                ▼
┌───────────────────────────────┐
│ Power Platform custom         │
│ connector                     │
│ One operation:                │
│ ExecuteWorkspaceFileOperation │
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│ On-premises data gateway      │
└───────────────┬───────────────┘
                │ local HTTP
                ▼
┌───────────────────────────────┐
│ FastAPI broker                │
│ - schema validation           │
│ - operation validation        │
│ - workspace resolution        │
│ - path security               │
│ - policy enforcement          │
│ - audit logging               │
└───────────┬───────────┬───────┘
            │           │
            │           └─────────────────┐
            ▼                             ▼
┌───────────────────────┐       ┌───────────────────────┐
│ Python filesystem     │       │ Restricted PowerShell │
│ handlers              │       │ process executor      │
└───────────┬───────────┘       └───────────┬───────────┘
            │                               │
            └───────────────┬───────────────┘
                            ▼
                 D:\CopilotPOC\Workspace
                            │
                            ▼
                 D:\CopilotPOC\.recycle
```

The on-premises data gateway is a Windows component that bridges Power Platform cloud services to local resources. Microsoft documents that the gateway uses outbound connectivity rather than requiring inbound network ports. The custom connector definition is imported in OpenAPI 2.0 format.

---

## 4. Why use workspace aliases

The API must not accept an arbitrary Windows root directory from the agent. It accepts a logical workspace name:

```json
{
  "workspace": "demo",
  "path": "docs\\example.txt"
}
```

The server resolves `demo` through local configuration:

```yaml
workspaces:
  demo:
    root: "D:\\CopilotPOC\\Workspace"
    recycle_root: "D:\\CopilotPOC\\.recycle\\demo"
```

Benefits:

- The custom connector schema does not change when new projects are added.
- Copilot does not need to know physical disk locations.
- Each workspace can have independent extensions, size limits, write permissions, and command policies.
- A malformed path cannot select another drive merely by changing the workspace field.
- Windows ACLs can restrict the server process to the approved roots.

The initial configuration contains only the confirmed `demo` workspace, but the configuration format supports multiple aliases without an API change.

---

## 5. Project structure

```text
copilot-file-broker/
├─ app/
│  ├─ __init__.py
│  ├─ main.py
│  ├─ models.py
│  ├─ config.py
│  ├─ errors.py
│  ├─ security.py
│  ├─ dispatcher.py
│  ├─ filesystem.py
│  ├─ powershell.py
│  ├─ audit.py
│  └─ response_factory.py
├─ config/
│  └─ workspaces.yaml
├─ swagger/
│  └─ api-definition.swagger.yaml
├─ tests/
│  ├─ conftest.py
│  ├─ test_security.py
│  ├─ test_filesystem.py
│  ├─ test_commands.py
│  └─ test_api.py
├─ logs/
│  └─ .gitkeep
├─ .env.example
├─ requirements.txt
├─ requirements-dev.txt
├─ run.ps1
└─ README.md
```

---

## 6. Runtime dependencies

### `requirements.txt`

```text
fastapi>=0.115,<1.0
uvicorn[standard]>=0.34,<1.0
pydantic>=2.10,<3.0
PyYAML>=6.0,<7.0
```

### `requirements-dev.txt`

```text
-r requirements.txt
pytest>=8.0,<9.0
pytest-cov>=6.0,<7.0
httpx>=0.28,<1.0
```

Version ranges should be reviewed and pinned before production deployment. They are not security guarantees.

---

## 7. Workspace configuration

### `config/workspaces.yaml`

```yaml
server:
  host: "127.0.0.1"
  port: 8000
  log_directory: "D:\\CopilotPOC\\Logs"
  default_timeout_seconds: 20
  maximum_timeout_seconds: 60
  maximum_stdout_characters: 100000
  maximum_stderr_characters: 20000

workspaces:
  demo:
    root: "D:\\CopilotPOC\\Workspace"
    recycle_root: "D:\\CopilotPOC\\.recycle\\demo"

    permissions:
      read: true
      create: true
      update: true
      delete: true
      move: true
      copy: true
      search: true
      execute_command: true

    policy:
      allowed_extensions:
        - ".txt"
        - ".md"
        - ".json"
        - ".yaml"
        - ".yml"
        - ".csv"
        - ".xml"
        - ".py"
        - ".ps1"
        - ".html"
        - ".css"
        - ".js"
        - ".ts"

      maximum_file_size_bytes: 5242880
      maximum_write_characters: 1000000
      maximum_search_results: 500
      maximum_search_depth: 20
      allow_hidden_items: false
      allow_reparse_points: false
      allow_workspace_root_operation: false

    command_policy:
      allowed_commands:
        - "Get-ChildItem"
        - "Get-Content"
        - "Test-Path"
        - "Get-Item"
        - "Select-String"

      maximum_arguments: 20
      allow_environment_variables: false
      allow_wildcards: false
```

For the MVP, structured handlers should perform writes. Restricted command mode is initially read-only because shell-based writes are harder to validate reliably. The API schema can support future allowlisted write commands, but the initial policy does not enable them.

---

## 8. API operation model

### 8.1 Operation enum

```text
CREATE_FILE
CREATE_DIRECTORY
READ_FILE
LIST_DIRECTORY
UPDATE_FILE
APPEND_FILE
REPLACE_TEXT
DELETE_FILE
DELETE_DIRECTORY
MOVE
COPY
SEARCH_FILES
SEARCH_CONTENT
GET_METADATA
EXISTS
EXECUTE_COMMAND
```

### 8.2 Single endpoint

```http
POST /api/v1/filesystem/execute
Content-Type: application/json
```

### 8.3 Fixed request schema

Every property below is explicitly declared for Power Platform. Only `operation` and `workspace` are globally required. The server enforces additional fields according to the selected operation.

```json
{
  "operation": "READ_FILE",
  "workspace": "demo",

  "path": "docs\\example.txt",
  "destinationPath": null,

  "content": null,
  "encoding": "utf-8",

  "overwrite": false,
  "appendNewLine": true,
  "createParentDirectories": false,

  "recursive": false,
  "force": false,

  "searchPattern": null,
  "searchText": null,
  "replacementText": null,
  "expectedOccurrences": 1,
  "replaceAll": false,
  "caseSensitive": false,
  "useRegex": false,
  "wholeWord": false,

  "includeFiles": true,
  "includeDirectories": false,
  "includeHidden": false,
  "fileExtension": null,
  "namePattern": null,

  "maxDepth": 10,
  "maxResults": 100,
  "skip": 0,

  "maxContentCharacters": 100000,
  "returnContent": true,
  "returnMetadata": true,
  "returnHash": false,

  "expectedHash": null,
  "expectedLastModifiedUtc": null,

  "shellCommand": null,
  "shellArguments": [],

  "reason": "Read an approved workspace file",
  "timeoutSeconds": 20,
  "correlationId": "copilot-request-123"
}
```

### 8.4 Fixed response schema

```json
{
  "success": true,
  "status": "COMPLETED",
  "operation": "READ_FILE",
  "operationId": "c464924d-2554-44b4-8f9f-7c0df6ab4771",
  "correlationId": "copilot-request-123",

  "workspace": "demo",
  "path": "docs\\example.txt",
  "destinationPath": null,

  "message": "File read successfully.",
  "errorCode": null,
  "errorMessage": null,

  "exists": true,
  "itemType": "FILE",

  "content": "Example content",
  "encoding": "utf-8",
  "contentTruncated": false,

  "name": "example.txt",
  "extension": ".txt",
  "sizeBytes": 15,
  "createdUtc": "2026-07-26T10:00:00Z",
  "modifiedUtc": "2026-07-26T10:00:00Z",
  "hash": null,

  "affectedCount": 1,
  "totalResults": 1,
  "returnedResults": 1,
  "hasMore": false,
  "nextSkip": null,

  "items": [],
  "matches": [],

  "exitCode": null,
  "stdout": "",
  "stderr": "",
  "outputTruncated": false,

  "recycleId": null,
  "recyclePath": null,

  "durationMs": 7,

  "policyAllowed": true,
  "policyRule": "workspace-file-read"
}
```

### 8.5 Status enum

```text
COMPLETED
PARTIAL
REJECTED
FAILED
TIMEOUT
CONFLICT
NOT_FOUND
```

### 8.6 Item type enum

```text
FILE
DIRECTORY
REPARSE_POINT
OTHER
```

Reparse points are returned only as metadata and are rejected as operation targets in the MVP.

---

## 9. Operation validation matrix

| Operation | Required request fields | Main optional fields |
|---|---|---|
| `CREATE_FILE` | `path`, `content` | `encoding`, `overwrite`, `createParentDirectories` |
| `CREATE_DIRECTORY` | `path` | `createParentDirectories` |
| `READ_FILE` | `path` | `encoding`, `maxContentCharacters`, `returnHash` |
| `LIST_DIRECTORY` | `path` | `recursive`, `includeFiles`, `includeDirectories`, `maxDepth`, `maxResults`, `skip` |
| `UPDATE_FILE` | `path`, `content` | `encoding`, `expectedHash`, `expectedLastModifiedUtc` |
| `APPEND_FILE` | `path`, `content` | `encoding`, `appendNewLine`, `expectedHash` |
| `REPLACE_TEXT` | `path`, `searchText`, `replacementText` | `caseSensitive`, `useRegex`, `wholeWord`, `expectedOccurrences`, `replaceAll`, `expectedHash` |
| `DELETE_FILE` | `path` | `expectedHash`, `reason` |
| `DELETE_DIRECTORY` | `path` | `recursive`, `reason` |
| `MOVE` | `path`, `destinationPath` | `overwrite`, `createParentDirectories` |
| `COPY` | `path`, `destinationPath` | `overwrite`, `recursive`, `createParentDirectories` |
| `SEARCH_FILES` | `path` | `searchPattern`, `namePattern`, `fileExtension`, `recursive`, `maxDepth`, `maxResults`, `skip` |
| `SEARCH_CONTENT` | `path`, `searchText` | `searchPattern`, `fileExtension`, `caseSensitive`, `useRegex`, `wholeWord`, `recursive`, `maxDepth`, `maxResults`, `skip` |
| `GET_METADATA` | `path` | `returnHash` |
| `EXISTS` | `path` | none |
| `EXECUTE_COMMAND` | `shellCommand` | `shellArguments`, `path`, `timeoutSeconds` |

The server must reject irrelevant dangerous combinations rather than silently forwarding them.

---

## 10. Python models

### `app/models.py`

```python
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
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

    content: str | None = None
    encoding: str = Field(default="utf-8", pattern=r"^(utf-8|utf-8-bom|ascii|unicode|base64)$")

    overwrite: bool = False
    appendNewLine: bool = True
    createParentDirectories: bool = False

    recursive: bool = False
    force: bool = False

    searchPattern: str | None = Field(default=None, max_length=200)
    searchText: str | None = None
    replacementText: str | None = None
    expectedOccurrences: int = Field(default=1, ge=0, le=10000)
    replaceAll: bool = False
    caseSensitive: bool = False
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

    expectedHash: str | None = Field(default=None, max_length=100)
    expectedLastModifiedUtc: datetime | None = None

    shellCommand: str | None = Field(default=None, max_length=100)
    shellArguments: list[str] = Field(default_factory=list, max_length=50)

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
```

FastAPI uses Pydantic models to validate and document request bodies. Defining `extra="forbid"` prevents undeclared JSON properties from being accepted.

---

## 11. Configuration loader

### `app/config.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml


@dataclass(frozen=True)
class Workspace:
    alias: str
    root: Path
    recycle_root: Path
    permissions: dict[str, bool]
    policy: dict[str, Any]
    command_policy: dict[str, Any]


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    log_directory: Path
    default_timeout_seconds: int
    maximum_timeout_seconds: int
    maximum_stdout_characters: int
    maximum_stderr_characters: int
    workspaces: dict[str, Workspace]


def load_config(path: Path) -> AppConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    server = raw["server"]

    workspaces: dict[str, Workspace] = {}
    for alias, value in raw["workspaces"].items():
        workspaces[alias] = Workspace(
            alias=alias,
            root=Path(value["root"]).resolve(strict=False),
            recycle_root=Path(value["recycle_root"]).resolve(strict=False),
            permissions=dict(value["permissions"]),
            policy=dict(value["policy"]),
            command_policy=dict(value["command_policy"]),
        )

    return AppConfig(
        host=server["host"],
        port=int(server["port"]),
        log_directory=Path(server["log_directory"]),
        default_timeout_seconds=int(server["default_timeout_seconds"]),
        maximum_timeout_seconds=int(server["maximum_timeout_seconds"]),
        maximum_stdout_characters=int(server["maximum_stdout_characters"]),
        maximum_stderr_characters=int(server["maximum_stderr_characters"]),
        workspaces=workspaces,
    )
```

---

## 12. Security and path validation

### 12.1 Required path rules

The server must reject:

- Absolute paths
- Drive-qualified paths such as `C:\...`
- UNC paths such as `\\server\share`
- Parent traversal such as `..\..\Windows`
- Alternate data streams such as `file.txt:secret`
- Workspace-root deletion or move
- Reparse-point traversal
- Paths resolving outside the workspace
- Disallowed extensions for file create or update
- Hidden items when the workspace policy disables them

### 12.2 Windows path resolver

### `app/security.py`

```python
from __future__ import annotations

import os
import stat
from pathlib import Path, PureWindowsPath

from app.config import Workspace


class PolicyViolation(Exception):
    def __init__(self, code: str, message: str, rule: str):
        super().__init__(message)
        self.code = code
        self.message = message
        self.rule = rule


def _is_reparse_point(path: Path) -> bool:
    try:
        attrs = os.lstat(path).st_file_attributes
    except (AttributeError, FileNotFoundError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def resolve_workspace_path(
    workspace: Workspace,
    relative_path: str | None,
    *,
    must_exist: bool = False,
    allow_root: bool = False,
) -> Path:
    if relative_path is None or not relative_path.strip():
        relative_path = "."

    raw = relative_path.strip()
    win_path = PureWindowsPath(raw)

    if win_path.is_absolute() or win_path.drive:
        raise PolicyViolation(
            "ABSOLUTE_PATH_DENIED",
            "Absolute or drive-qualified paths are not permitted.",
            "deny-absolute-path",
        )

    if raw.startswith("\\\\") or raw.startswith("//"):
        raise PolicyViolation(
            "UNC_PATH_DENIED",
            "UNC paths are not permitted.",
            "deny-unc-path",
        )

    if any(part == ".." for part in win_path.parts):
        raise PolicyViolation(
            "PATH_TRAVERSAL_DENIED",
            "Parent-directory traversal is not permitted.",
            "deny-parent-traversal",
        )

    # Deny NTFS alternate data stream syntax. The drive check above has
    # already accounted for a normal drive colon.
    if ":" in raw:
        raise PolicyViolation(
            "ALTERNATE_DATA_STREAM_DENIED",
            "Colon characters are not permitted in relative paths.",
            "deny-alternate-data-stream",
        )

    root = workspace.root.resolve(strict=False)
    candidate = (root / Path(*win_path.parts)).resolve(strict=False)

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PolicyViolation(
            "WORKSPACE_ESCAPE_DENIED",
            "The resolved path is outside the configured workspace.",
            "deny-workspace-escape",
        ) from exc

    if candidate == root and not allow_root:
        raise PolicyViolation(
            "WORKSPACE_ROOT_OPERATION_DENIED",
            "This operation is not allowed on the workspace root.",
            "deny-workspace-root-operation",
        )

    # Check existing ancestors for junctions, symlinks, or other reparse points.
    current = root
    relative_parts = candidate.relative_to(root).parts
    for part in relative_parts:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            raise PolicyViolation(
                "REPARSE_POINT_DENIED",
                "Operations through symbolic links or reparse points are not permitted.",
                "deny-reparse-point",
            )

    if must_exist and not candidate.exists():
        raise FileNotFoundError(candidate)

    return candidate


def validate_extension(workspace: Workspace, path: Path) -> None:
    allowed = {
        extension.casefold()
        for extension in workspace.policy.get("allowed_extensions", [])
    }

    if path.suffix.casefold() not in allowed:
        raise PolicyViolation(
            "EXTENSION_DENIED",
            f"The extension '{path.suffix}' is not allowed.",
            "allowlisted-file-extensions",
        )
```

`Path.resolve(strict=False)` is useful for normalization, but it is not a complete security boundary. The explicit workspace containment check and ancestor reparse-point checks are required. Windows ACLs remain the final boundary.

---

## 13. Structured filesystem implementation

### `app/filesystem.py`

```python
from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import Workspace
from app.models import ContentMatch, FileSystemItem, ItemType
from app.security import PolicyViolation, resolve_workspace_path, validate_extension


def utc_from_timestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def item_type(path: Path) -> ItemType:
    if path.is_symlink():
        return ItemType.REPARSE_POINT
    if path.is_file():
        return ItemType.FILE
    if path.is_dir():
        return ItemType.DIRECTORY
    return ItemType.OTHER


def make_item(workspace: Workspace, path: Path, include_hash: bool = False) -> FileSystemItem:
    stats = path.stat()
    return FileSystemItem(
        name=path.name,
        relativePath=str(path.relative_to(workspace.root)),
        itemType=item_type(path),
        extension=path.suffix or None,
        sizeBytes=stats.st_size if path.is_file() else None,
        createdUtc=utc_from_timestamp(stats.st_ctime),
        modifiedUtc=utc_from_timestamp(stats.st_mtime),
        isHidden=path.name.startswith("."),
        isReadOnly=not os.access(path, os.W_OK),
        hash=sha256_file(path) if include_hash and path.is_file() else None,
    )


def decode_request_content(content: str, encoding: str) -> bytes:
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except ValueError as exc:
            raise PolicyViolation(
                "INVALID_BASE64",
                "The supplied content is not valid Base64.",
                "validate-content-encoding",
            ) from exc

    codec = {
        "utf-8": "utf-8",
        "utf-8-bom": "utf-8-sig",
        "ascii": "ascii",
        "unicode": "utf-16",
    }[encoding]
    return content.encode(codec)


def encode_response_content(data: bytes, encoding: str) -> str:
    if encoding == "base64":
        return base64.b64encode(data).decode("ascii")

    codec = {
        "utf-8": "utf-8",
        "utf-8-bom": "utf-8-sig",
        "ascii": "ascii",
        "unicode": "utf-16",
    }[encoding]
    return data.decode(codec)


def check_expected_version(
    path: Path,
    expected_hash: str | None,
    expected_modified: datetime | None,
) -> None:
    if expected_hash and sha256_file(path) != expected_hash:
        raise PolicyViolation(
            "HASH_MISMATCH",
            "The file changed after it was read.",
            "optimistic-concurrency-hash",
        )

    if expected_modified:
        actual = utc_from_timestamp(path.stat().st_mtime)
        if abs((actual - expected_modified).total_seconds()) > 0.001:
            raise PolicyViolation(
                "LAST_MODIFIED_MISMATCH",
                "The file modification timestamp has changed.",
                "optimistic-concurrency-timestamp",
            )


def create_file(
    workspace: Workspace,
    relative_path: str,
    content: str,
    encoding: str,
    overwrite: bool,
    create_parents: bool,
) -> Path:
    target = resolve_workspace_path(workspace, relative_path)
    validate_extension(workspace, target)

    if target.exists() and not overwrite:
        raise FileExistsError(target)

    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.parent.exists():
        raise FileNotFoundError(target.parent)

    data = decode_request_content(content, encoding)
    maximum = int(workspace.policy["maximum_file_size_bytes"])
    if len(data) > maximum:
        raise PolicyViolation(
            "FILE_SIZE_LIMIT_EXCEEDED",
            "The file exceeds the configured size limit.",
            "maximum-file-size",
        )

    target.write_bytes(data)
    return target


def read_file(
    workspace: Workspace,
    relative_path: str,
    encoding: str,
    max_characters: int,
) -> tuple[Path, str, bool]:
    target = resolve_workspace_path(workspace, relative_path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(target)

    maximum = int(workspace.policy["maximum_file_size_bytes"])
    if target.stat().st_size > maximum:
        raise PolicyViolation(
            "FILE_SIZE_LIMIT_EXCEEDED",
            "The file exceeds the configured read size limit.",
            "maximum-file-size",
        )

    value = encode_response_content(target.read_bytes(), encoding)
    truncated = len(value) > max_characters
    return target, value[:max_characters], truncated


def update_file(
    workspace: Workspace,
    relative_path: str,
    content: str,
    encoding: str,
    expected_hash: str | None,
    expected_modified: datetime | None,
) -> Path:
    target = resolve_workspace_path(workspace, relative_path, must_exist=True)
    validate_extension(workspace, target)

    if not target.is_file():
        raise IsADirectoryError(target)

    check_expected_version(target, expected_hash, expected_modified)
    data = decode_request_content(content, encoding)

    maximum = int(workspace.policy["maximum_file_size_bytes"])
    if len(data) > maximum:
        raise PolicyViolation(
            "FILE_SIZE_LIMIT_EXCEEDED",
            "The updated file exceeds the configured size limit.",
            "maximum-file-size",
        )

    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)
    return target


def append_file(
    workspace: Workspace,
    relative_path: str,
    content: str,
    encoding: str,
    append_newline: bool,
    expected_hash: str | None,
) -> Path:
    target = resolve_workspace_path(workspace, relative_path, must_exist=True)
    validate_extension(workspace, target)
    check_expected_version(target, expected_hash, None)

    suffix = "\r\n" if append_newline else ""
    data = decode_request_content(content + suffix, encoding)

    maximum = int(workspace.policy["maximum_file_size_bytes"])
    if target.stat().st_size + len(data) > maximum:
        raise PolicyViolation(
            "FILE_SIZE_LIMIT_EXCEEDED",
            "Appending would exceed the configured size limit.",
            "maximum-file-size",
        )

    with target.open("ab") as stream:
        stream.write(data)
    return target


def replace_text(
    workspace: Workspace,
    relative_path: str,
    search_text: str,
    replacement_text: str,
    *,
    encoding: str,
    case_sensitive: bool,
    use_regex: bool,
    whole_word: bool,
    expected_occurrences: int,
    replace_all: bool,
    expected_hash: str | None,
) -> tuple[Path, int]:
    target, text, truncated = read_file(
        workspace,
        relative_path,
        encoding,
        int(workspace.policy["maximum_write_characters"]),
    )
    if truncated:
        raise PolicyViolation(
            "CONTENT_LIMIT_EXCEEDED",
            "The file is too large for text replacement.",
            "maximum-write-characters",
        )

    check_expected_version(target, expected_hash, None)

    pattern_text = search_text if use_regex else re.escape(search_text)
    if whole_word:
        pattern_text = rf"\b(?:{pattern_text})\b"

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(pattern_text, flags)
    matches = list(pattern.finditer(text))
    count = len(matches)

    if count != expected_occurrences:
        raise PolicyViolation(
            "UNEXPECTED_MATCH_COUNT",
            f"Expected {expected_occurrences} match(es), found {count}.",
            "expected-replacement-occurrences",
        )

    maximum_replacements = 0 if replace_all else 1
    updated, replacements = pattern.subn(
        replacement_text,
        text,
        count=maximum_replacements,
    )

    update_file(
        workspace,
        relative_path,
        updated,
        encoding,
        expected_hash,
        None,
    )
    return target, replacements


def move_to_recycle(workspace: Workspace, relative_path: str) -> tuple[Path, str, Path]:
    source = resolve_workspace_path(workspace, relative_path, must_exist=True)

    recycle_id = uuid.uuid4().hex
    date_part = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    relative = source.relative_to(workspace.root)
    destination = workspace.recycle_root / date_part / recycle_id / relative

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    return source, recycle_id, destination


def walk_limited(root: Path, max_depth: int):
    base_depth = len(root.parts)
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.parts) - base_depth

        if depth >= max_depth:
            directories[:] = []

        yield current_path, directories, files


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
) -> tuple[list[FileSystemItem], int]:
    root = resolve_workspace_path(
        workspace,
        relative_path,
        must_exist=True,
        allow_root=True,
    )
    if not root.is_dir():
        raise NotADirectoryError(root)

    candidates: list[Path] = []

    if recursive:
        for current, directories, files in walk_limited(root, max_depth):
            if include_directories:
                candidates.extend(current / name for name in directories)
            if include_files:
                candidates.extend(current / name for name in files)
    else:
        candidates = [
            item for item in root.iterdir()
            if (item.is_file() and include_files)
            or (item.is_dir() and include_directories)
        ]

    def matches(path: Path) -> bool:
        if search_pattern and not fnmatch.fnmatch(path.name, search_pattern):
            return False
        if name_pattern and not fnmatch.fnmatch(path.name, name_pattern):
            return False
        if extension and path.suffix.casefold() != extension.casefold():
            return False
        return True

    filtered = [path for path in candidates if matches(path)]
    total = len(filtered)
    page = filtered[skip : skip + max_results]
    return [make_item(workspace, path) for path in page], total


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
) -> tuple[list[ContentMatch], int]:
    files, _ = search_files(
        workspace,
        relative_path,
        recursive=recursive,
        max_depth=max_depth,
        search_pattern=search_pattern,
        name_pattern=None,
        extension=extension,
        include_files=True,
        include_directories=False,
        max_results=1000,
        skip=0,
    )

    expression = search_text if use_regex else re.escape(search_text)
    if whole_word:
        expression = rf"\b(?:{expression})\b"

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(expression)

    all_matches: list[ContentMatch] = []
    for item in files:
        path = workspace.root / item.relativePath
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue

        for line_number, line in enumerate(lines, start=1):
            for match in pattern.finditer(line):
                all_matches.append(
                    ContentMatch(
                        relativePath=item.relativePath,
                        lineNumber=line_number,
                        columnNumber=match.start() + 1,
                        matchedText=match.group(0),
                        lineText=line[:1000],
                    )
                )

    total = len(all_matches)
    return all_matches[skip : skip + max_results], total
```

Production code should split this module further, but keeping the handlers together makes the proof of concept easier to review.

---

## 14. Restricted PowerShell execution

### 14.1 Security rules

`EXECUTE_COMMAND` accepts:

```json
{
  "operation": "EXECUTE_COMMAND",
  "workspace": "demo",
  "shellCommand": "Get-ChildItem",
  "shellArguments": [
    "-LiteralPath",
    ".",
    "-File"
  ]
}
```

It does not accept:

```json
{
  "script": "Get-ChildItem; Remove-Item -Recurse C:\\"
}
```

The server must enforce:

- Exact command-name allowlist
- Explicit argument array
- No newline characters
- No semicolons
- No pipes
- No redirection
- No `&`
- No backticks
- No `$()` or `${}`
- No encoded command
- No nested `powershell.exe`, `pwsh.exe`, or `cmd.exe`
- No executable paths
- No environment-variable expansion
- No absolute or UNC path arguments
- Maximum argument count and length
- Process timeout
- Captured and truncated stdout/stderr
- Workspace as the process working directory
- Windows process account restricted by NTFS ACLs

### 14.2 PowerShell executor

### `app/powershell.py`

```python
from __future__ import annotations

import subprocess
from pathlib import PureWindowsPath

from app.config import AppConfig, Workspace
from app.security import PolicyViolation


FORBIDDEN_ARGUMENT_TOKENS = (
    "\r",
    "\n",
    ";",
    "|",
    ">",
    "<",
    "&",
    "`",
    "$(",
    "${",
)

PATH_PARAMETER_NAMES = {
    "-Path",
    "-LiteralPath",
}


def validate_command(workspace: Workspace, command: str, arguments: list[str]) -> None:
    allowed = {
        value.casefold(): value
        for value in workspace.command_policy["allowed_commands"]
    }

    if command.casefold() not in allowed:
        raise PolicyViolation(
            "COMMAND_DENIED",
            f"The command '{command}' is not allowlisted.",
            "allowlisted-powershell-commands",
        )

    maximum_arguments = int(workspace.command_policy["maximum_arguments"])
    if len(arguments) > maximum_arguments:
        raise PolicyViolation(
            "TOO_MANY_ARGUMENTS",
            "The command has too many arguments.",
            "maximum-command-arguments",
        )

    for argument in arguments:
        if len(argument) > 1000:
            raise PolicyViolation(
                "ARGUMENT_TOO_LONG",
                "A command argument exceeds the allowed length.",
                "maximum-command-argument-length",
            )

        if any(token in argument for token in FORBIDDEN_ARGUMENT_TOKENS):
            raise PolicyViolation(
                "COMMAND_TOKEN_DENIED",
                "A command argument contains a forbidden shell token.",
                "deny-shell-metacharacters",
            )

        if "%" in argument and not workspace.command_policy.get(
            "allow_environment_variables",
            False,
        ):
            raise PolicyViolation(
                "ENVIRONMENT_EXPANSION_DENIED",
                "Environment-variable syntax is not permitted.",
                "deny-environment-expansion",
            )

    # Validate values that follow common PowerShell path parameters.
    for index, argument in enumerate(arguments[:-1]):
        if argument in PATH_PARAMETER_NAMES:
            value = arguments[index + 1]
            path = PureWindowsPath(value)
            if path.is_absolute() or path.drive or value.startswith("\\\\"):
                raise PolicyViolation(
                    "COMMAND_ABSOLUTE_PATH_DENIED",
                    "Command path arguments must be relative.",
                    "deny-command-absolute-path",
                )
            if ".." in path.parts:
                raise PolicyViolation(
                    "COMMAND_PATH_TRAVERSAL_DENIED",
                    "Command path traversal is not permitted.",
                    "deny-command-path-traversal",
                )


def build_wrapper_command(command: str, arguments: list[str]) -> list[str]:
    # The command name is already allowlisted. Arguments are passed after `--%`
    # is intentionally NOT used because it complicates validation. PowerShell
    # receives a fixed invocation and explicit quoted literals.
    escaped = []
    for value in arguments:
        escaped.append("'" + value.replace("'", "''") + "'")

    invocation = "& " + command
    if escaped:
        invocation += " " + " ".join(escaped)

    return [
        "pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "AllSigned",
        "-Command",
        invocation,
    ]


def execute_restricted_command(
    app_config: AppConfig,
    workspace: Workspace,
    command: str,
    arguments: list[str],
    timeout_seconds: int,
) -> tuple[int, str, str, bool]:
    validate_command(workspace, command, arguments)

    command_line = build_wrapper_command(command, arguments)
    timeout = min(timeout_seconds, app_config.maximum_timeout_seconds)

    try:
        completed = subprocess.run(
            command_line,
            cwd=workspace.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env={
                "PATH": r"C:\Program Files\PowerShell\7",
                "TEMP": r"D:\CopilotPOC\Temp",
                "TMP": r"D:\CopilotPOC\Temp",
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("PowerShell command timed out.") from exc

    stdout_limit = app_config.maximum_stdout_characters
    stderr_limit = app_config.maximum_stderr_characters

    output_truncated = (
        len(completed.stdout) > stdout_limit
        or len(completed.stderr) > stderr_limit
    )

    return (
        completed.returncode,
        completed.stdout[:stdout_limit],
        completed.stderr[:stderr_limit],
        output_truncated,
    )
```

### Important implementation note

Even an allowlisted command can expose more than intended through complex parameters. The command policy must therefore validate each command’s permitted parameters, not only its name.

A stronger follow-up implementation should replace the generic argument validator with per-command profiles:

```python
COMMAND_PROFILES = {
    "Get-ChildItem": {
        "allowed_switches": {
            "-LiteralPath",
            "-File",
            "-Directory",
            "-Recurse",
            "-Name",
            "-Force",
        },
        "denied_switches": {
            "-Filter",  # enable later only after validation
        },
    },
    "Get-Content": {
        "allowed_switches": {
            "-LiteralPath",
            "-Raw",
            "-TotalCount",
        },
    },
}
```

For the initial demonstration, restricted command mode should remain read-only and structured handlers should implement all writes.

---

## 15. Dispatcher

### `app/dispatcher.py`

```python
from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from app.config import AppConfig, Workspace
from app.filesystem import (
    append_file,
    create_file,
    make_item,
    move_to_recycle,
    read_file,
    replace_text,
    search_content,
    search_files,
    sha256_file,
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
from app.security import (
    PolicyViolation,
    resolve_workspace_path,
    validate_extension,
)


class RequestValidationError(Exception):
    pass


def require(value, field_name: str):
    if value is None:
        raise RequestValidationError(
            f"Field '{field_name}' is required for this operation."
        )
    return value


def permission_for(operation: Operation) -> str:
    mapping = {
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
    return mapping[operation]


def dispatch(
    app_config: AppConfig,
    request: FileOperationRequest,
) -> FileOperationResponse:
    started = time.perf_counter()
    operation_id = str(uuid.uuid4())

    if request.workspace not in app_config.workspaces:
        raise PolicyViolation(
            "WORKSPACE_NOT_FOUND",
            "The requested workspace alias is not configured.",
            "configured-workspaces-only",
        )

    workspace = app_config.workspaces[request.workspace]
    permission = permission_for(request.operation)

    if not workspace.permissions.get(permission, False):
        raise PolicyViolation(
            "OPERATION_DENIED",
            "The selected workspace does not permit this operation.",
            f"workspace-permission-{permission}",
        )

    base = dict(
        success=True,
        status=Status.COMPLETED,
        operation=request.operation.value,
        operationId=operation_id,
        correlationId=request.correlationId,
        workspace=request.workspace,
        path=request.path,
        destinationPath=request.destinationPath,
        message="Operation completed.",
        durationMs=0,
        policyAllowed=True,
        policyRule=f"workspace-{permission}",
    )

    if request.operation == Operation.CREATE_FILE:
        target = create_file(
            workspace,
            require(request.path, "path"),
            require(request.content, "content"),
            request.encoding,
            request.overwrite,
            request.createParentDirectories,
        )
        item = make_item(workspace, target, request.returnHash)
        response = FileOperationResponse(
            **base,
            message="File created successfully.",
            exists=True,
            itemType=ItemType.FILE,
            name=item.name,
            extension=item.extension,
            sizeBytes=item.sizeBytes,
            createdUtc=item.createdUtc,
            modifiedUtc=item.modifiedUtc,
            hash=item.hash,
            affectedCount=1,
        )

    elif request.operation == Operation.CREATE_DIRECTORY:
        target = resolve_workspace_path(workspace, require(request.path, "path"))
        target.mkdir(
            parents=request.createParentDirectories,
            exist_ok=request.overwrite,
        )
        response = FileOperationResponse(
            **base,
            message="Directory created successfully.",
            exists=True,
            itemType=ItemType.DIRECTORY,
            name=target.name,
            affectedCount=1,
        )

    elif request.operation == Operation.READ_FILE:
        target, content, truncated = read_file(
            workspace,
            require(request.path, "path"),
            request.encoding,
            request.maxContentCharacters,
        )
        item = make_item(workspace, target, request.returnHash)
        response = FileOperationResponse(
            **base,
            message="File read successfully.",
            exists=True,
            itemType=ItemType.FILE,
            content=content if request.returnContent else None,
            encoding=request.encoding,
            contentTruncated=truncated,
            name=item.name,
            extension=item.extension,
            sizeBytes=item.sizeBytes,
            createdUtc=item.createdUtc,
            modifiedUtc=item.modifiedUtc,
            hash=item.hash,
            affectedCount=1,
            totalResults=1,
            returnedResults=1,
        )

    elif request.operation == Operation.LIST_DIRECTORY:
        items, total = search_files(
            workspace,
            require(request.path, "path"),
            recursive=request.recursive,
            max_depth=request.maxDepth,
            search_pattern=None,
            name_pattern=None,
            extension=None,
            include_files=request.includeFiles,
            include_directories=request.includeDirectories,
            max_results=request.maxResults,
            skip=request.skip,
        )
        response = FileOperationResponse(
            **base,
            message="Directory listed successfully.",
            exists=True,
            itemType=ItemType.DIRECTORY,
            items=items,
            affectedCount=len(items),
            totalResults=total,
            returnedResults=len(items),
            hasMore=request.skip + len(items) < total,
            nextSkip=(
                request.skip + len(items)
                if request.skip + len(items) < total
                else None
            ),
        )

    elif request.operation == Operation.UPDATE_FILE:
        target = update_file(
            workspace,
            require(request.path, "path"),
            require(request.content, "content"),
            request.encoding,
            request.expectedHash,
            request.expectedLastModifiedUtc,
        )
        item = make_item(workspace, target, request.returnHash)
        response = FileOperationResponse(
            **base,
            message="File updated successfully.",
            exists=True,
            itemType=ItemType.FILE,
            name=item.name,
            extension=item.extension,
            sizeBytes=item.sizeBytes,
            modifiedUtc=item.modifiedUtc,
            hash=item.hash,
            affectedCount=1,
        )

    elif request.operation == Operation.APPEND_FILE:
        target = append_file(
            workspace,
            require(request.path, "path"),
            require(request.content, "content"),
            request.encoding,
            request.appendNewLine,
            request.expectedHash,
        )
        item = make_item(workspace, target, request.returnHash)
        response = FileOperationResponse(
            **base,
            message="Content appended successfully.",
            exists=True,
            itemType=ItemType.FILE,
            sizeBytes=item.sizeBytes,
            modifiedUtc=item.modifiedUtc,
            hash=item.hash,
            affectedCount=1,
        )

    elif request.operation == Operation.REPLACE_TEXT:
        target, count = replace_text(
            workspace,
            require(request.path, "path"),
            require(request.searchText, "searchText"),
            require(request.replacementText, "replacementText"),
            encoding=request.encoding,
            case_sensitive=request.caseSensitive,
            use_regex=request.useRegex,
            whole_word=request.wholeWord,
            expected_occurrences=request.expectedOccurrences,
            replace_all=request.replaceAll,
            expected_hash=request.expectedHash,
        )
        response = FileOperationResponse(
            **base,
            message=f"Replaced {count} occurrence(s).",
            exists=True,
            itemType=ItemType.FILE,
            affectedCount=count,
        )

    elif request.operation in {
        Operation.DELETE_FILE,
        Operation.DELETE_DIRECTORY,
    }:
        source = resolve_workspace_path(
            workspace,
            require(request.path, "path"),
            must_exist=True,
        )

        if request.operation == Operation.DELETE_FILE and not source.is_file():
            raise RequestValidationError("DELETE_FILE requires a file target.")

        if request.operation == Operation.DELETE_DIRECTORY and not source.is_dir():
            raise RequestValidationError(
                "DELETE_DIRECTORY requires a directory target."
            )

        _, recycle_id, destination = move_to_recycle(
            workspace,
            require(request.path, "path"),
        )
        response = FileOperationResponse(
            **base,
            message="Item moved to the recycle directory.",
            exists=False,
            affectedCount=1,
            recycleId=recycle_id,
            recyclePath=str(destination),
        )

    elif request.operation == Operation.MOVE:
        source = resolve_workspace_path(
            workspace,
            require(request.path, "path"),
            must_exist=True,
        )
        destination = resolve_workspace_path(
            workspace,
            require(request.destinationPath, "destinationPath"),
        )

        if destination.exists() and not request.overwrite:
            raise FileExistsError(destination)

        if request.createParentDirectories:
            destination.parent.mkdir(parents=True, exist_ok=True)

        shutil.move(str(source), str(destination))
        response = FileOperationResponse(
            **base,
            message="Item moved successfully.",
            exists=True,
            affectedCount=1,
        )

    elif request.operation == Operation.COPY:
        source = resolve_workspace_path(
            workspace,
            require(request.path, "path"),
            must_exist=True,
        )
        destination = resolve_workspace_path(
            workspace,
            require(request.destinationPath, "destinationPath"),
        )

        if destination.exists() and not request.overwrite:
            raise FileExistsError(destination)

        if request.createParentDirectories:
            destination.parent.mkdir(parents=True, exist_ok=True)

        if source.is_dir():
            if not request.recursive:
                raise RequestValidationError(
                    "Copying a directory requires recursive=true."
                )
            shutil.copytree(
                source,
                destination,
                dirs_exist_ok=request.overwrite,
            )
        else:
            validate_extension(workspace, destination)
            shutil.copy2(source, destination)

        response = FileOperationResponse(
            **base,
            message="Item copied successfully.",
            exists=True,
            affectedCount=1,
        )

    elif request.operation == Operation.SEARCH_FILES:
        items, total = search_files(
            workspace,
            require(request.path, "path"),
            recursive=request.recursive,
            max_depth=request.maxDepth,
            search_pattern=request.searchPattern,
            name_pattern=request.namePattern,
            extension=request.fileExtension,
            include_files=request.includeFiles,
            include_directories=request.includeDirectories,
            max_results=request.maxResults,
            skip=request.skip,
        )
        response = FileOperationResponse(
            **base,
            message="File search completed.",
            items=items,
            affectedCount=len(items),
            totalResults=total,
            returnedResults=len(items),
            hasMore=request.skip + len(items) < total,
            nextSkip=(
                request.skip + len(items)
                if request.skip + len(items) < total
                else None
            ),
        )

    elif request.operation == Operation.SEARCH_CONTENT:
        matches, total = search_content(
            workspace,
            require(request.path, "path"),
            require(request.searchText, "searchText"),
            recursive=request.recursive,
            max_depth=request.maxDepth,
            search_pattern=request.searchPattern,
            extension=request.fileExtension,
            case_sensitive=request.caseSensitive,
            use_regex=request.useRegex,
            whole_word=request.wholeWord,
            max_results=request.maxResults,
            skip=request.skip,
        )
        response = FileOperationResponse(
            **base,
            message="Content search completed.",
            matches=matches,
            affectedCount=len(matches),
            totalResults=total,
            returnedResults=len(matches),
            hasMore=request.skip + len(matches) < total,
            nextSkip=(
                request.skip + len(matches)
                if request.skip + len(matches) < total
                else None
            ),
        )

    elif request.operation == Operation.GET_METADATA:
        target = resolve_workspace_path(
            workspace,
            require(request.path, "path"),
            must_exist=True,
        )
        item = make_item(workspace, target, request.returnHash)
        response = FileOperationResponse(
            **base,
            message="Metadata read successfully.",
            exists=True,
            itemType=item.itemType,
            name=item.name,
            extension=item.extension,
            sizeBytes=item.sizeBytes,
            createdUtc=item.createdUtc,
            modifiedUtc=item.modifiedUtc,
            hash=item.hash,
            affectedCount=1,
        )

    elif request.operation == Operation.EXISTS:
        target = resolve_workspace_path(
            workspace,
            require(request.path, "path"),
        )
        response = FileOperationResponse(
            **base,
            message="Existence check completed.",
            exists=target.exists(),
            itemType=(
                make_item(workspace, target).itemType
                if target.exists()
                else None
            ),
            affectedCount=1 if target.exists() else 0,
        )

    elif request.operation == Operation.EXECUTE_COMMAND:
        command = require(request.shellCommand, "shellCommand")
        exit_code, stdout, stderr, truncated = execute_restricted_command(
            app_config,
            workspace,
            command,
            request.shellArguments,
            request.timeoutSeconds,
        )
        response = FileOperationResponse(
            **base,
            success=exit_code == 0,
            status=(
                Status.COMPLETED
                if exit_code == 0
                else Status.FAILED
            ),
            message=(
                "Command completed."
                if exit_code == 0
                else "Command returned a non-zero exit code."
            ),
            exitCode=exit_code,
            stdout=stdout,
            stderr=stderr,
            outputTruncated=truncated,
        )

    else:
        raise RequestValidationError("Unsupported operation.")

    response.durationMs = int((time.perf_counter() - started) * 1000)
    return response
```

---

## 16. Audit logging

### `app/audit.py`

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from app.models import FileOperationRequest, FileOperationResponse


_lock = Lock()


def write_audit_log(
    log_directory: Path,
    request: FileOperationRequest,
    response: FileOperationResponse,
) -> None:
    log_directory.mkdir(parents=True, exist_ok=True)
    path = log_directory / f"audit-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"

    record = {
        "timestampUtc": datetime.now(timezone.utc).isoformat(),
        "operationId": response.operationId,
        "correlationId": request.correlationId,
        "workspace": request.workspace,
        "operation": request.operation.value,
        "path": request.path,
        "destinationPath": request.destinationPath,
        "shellCommand": request.shellCommand,
        "shellArgumentCount": len(request.shellArguments),
        "reason": request.reason,
        "success": response.success,
        "status": response.status.value,
        "errorCode": response.errorCode,
        "durationMs": response.durationMs,
        "policyAllowed": response.policyAllowed,
        "policyRule": response.policyRule,
    }

    # Do not log request.content, response.content, stdout, or stderr by default.
    serialized = json.dumps(record, ensure_ascii=False)

    with _lock:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
```

The audit log deliberately excludes file contents and command output to avoid creating a second uncontrolled copy of sensitive data.

---

## 17. FastAPI application

### `app/main.py`

```python
from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.audit import write_audit_log
from app.config import load_config
from app.dispatcher import RequestValidationError, dispatch
from app.models import (
    FileOperationRequest,
    FileOperationResponse,
    Status,
)
from app.security import PolicyViolation


CONFIG = load_config(Path("config/workspaces.yaml"))

app = FastAPI(
    title="Copilot Local Filesystem Broker",
    version="1.0.0",
    description=(
        "Executes approved filesystem operations inside configured "
        "Windows workspaces."
    ),
)


def error_response(
    request: FileOperationRequest,
    *,
    http_status: int,
    status: Status,
    error_code: str,
    message: str,
    policy_allowed: bool,
    policy_rule: str | None = None,
) -> JSONResponse:
    response = FileOperationResponse(
        success=False,
        status=status,
        operation=request.operation.value,
        operationId=str(uuid.uuid4()),
        correlationId=request.correlationId,
        workspace=request.workspace,
        path=request.path,
        destinationPath=request.destinationPath,
        message=message,
        errorCode=error_code,
        errorMessage=message,
        policyAllowed=policy_allowed,
        policyRule=policy_rule,
    )

    write_audit_log(CONFIG.log_directory, request, response)
    return JSONResponse(
        status_code=http_status,
        content=response.model_dump(mode="json"),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "healthy",
        "service": "copilot-local-filesystem-broker",
    }


@app.post(
    "/api/v1/filesystem/execute",
    response_model=FileOperationResponse,
)
def execute(request: FileOperationRequest):
    try:
        response = dispatch(CONFIG, request)
    except PolicyViolation as exc:
        status = (
            Status.CONFLICT
            if exc.code in {"HASH_MISMATCH", "LAST_MODIFIED_MISMATCH"}
            else Status.REJECTED
        )
        http_status = 409 if status == Status.CONFLICT else 403
        return error_response(
            request,
            http_status=http_status,
            status=status,
            error_code=exc.code,
            message=exc.message,
            policy_allowed=False,
            policy_rule=exc.rule,
        )
    except FileNotFoundError:
        return error_response(
            request,
            http_status=404,
            status=Status.NOT_FOUND,
            error_code="ITEM_NOT_FOUND",
            message="The requested file or directory was not found.",
            policy_allowed=True,
        )
    except FileExistsError:
        return error_response(
            request,
            http_status=409,
            status=Status.CONFLICT,
            error_code="ITEM_ALREADY_EXISTS",
            message="The destination already exists.",
            policy_allowed=True,
        )
    except RequestValidationError as exc:
        return error_response(
            request,
            http_status=400,
            status=Status.FAILED,
            error_code="INVALID_OPERATION_FIELDS",
            message=str(exc),
            policy_allowed=True,
        )
    except TimeoutError:
        return error_response(
            request,
            http_status=408,
            status=Status.TIMEOUT,
            error_code="EXECUTION_TIMEOUT",
            message="The operation exceeded its time limit.",
            policy_allowed=True,
        )
    except Exception:
        # Log the stack trace through Python logging in the real implementation,
        # but do not expose it to Copilot.
        return error_response(
            request,
            http_status=500,
            status=Status.FAILED,
            error_code="INTERNAL_ERROR",
            message="The operation failed because of an internal server error.",
            policy_allowed=True,
        )

    write_audit_log(CONFIG.log_directory, request, response)
    return response
```

---

## 18. Detailed request and response examples

### 18.1 Create a file

Request:

```json
{
  "operation": "CREATE_FILE",
  "workspace": "demo",
  "path": "notes\\copilot-demo.txt",
  "content": "Created by Microsoft Copilot.",
  "encoding": "utf-8",
  "overwrite": false,
  "createParentDirectories": true,
  "reason": "Create the proof-of-concept file",
  "correlationId": "demo-create-001"
}
```

Response:

```json
{
  "success": true,
  "status": "COMPLETED",
  "operation": "CREATE_FILE",
  "operationId": "f50a6dc6-9dc3-46da-a22b-0d2c45713c18",
  "correlationId": "demo-create-001",
  "workspace": "demo",
  "path": "notes\\copilot-demo.txt",
  "message": "File created successfully.",
  "exists": true,
  "itemType": "FILE",
  "name": "copilot-demo.txt",
  "extension": ".txt",
  "sizeBytes": 29,
  "affectedCount": 1,
  "items": [],
  "matches": [],
  "stdout": "",
  "stderr": "",
  "durationMs": 9,
  "policyAllowed": true,
  "policyRule": "workspace-create"
}
```

### 18.2 Read a file and return its hash

```json
{
  "operation": "READ_FILE",
  "workspace": "demo",
  "path": "notes\\copilot-demo.txt",
  "encoding": "utf-8",
  "maxContentCharacters": 100000,
  "returnContent": true,
  "returnMetadata": true,
  "returnHash": true,
  "correlationId": "demo-read-001"
}
```

The returned hash can be sent in a later update request as `expectedHash`.

### 18.3 Update a file safely

```json
{
  "operation": "UPDATE_FILE",
  "workspace": "demo",
  "path": "notes\\copilot-demo.txt",
  "content": "Updated by Microsoft Copilot.",
  "encoding": "utf-8",
  "expectedHash": "sha256:replace-with-hash-from-read",
  "returnHash": true,
  "reason": "Update the proof-of-concept file",
  "correlationId": "demo-update-001"
}
```

A hash mismatch returns HTTP 409 with:

```json
{
  "success": false,
  "status": "CONFLICT",
  "errorCode": "HASH_MISMATCH",
  "message": "The file changed after it was read."
}
```

### 18.4 Append content

```json
{
  "operation": "APPEND_FILE",
  "workspace": "demo",
  "path": "notes\\copilot-demo.txt",
  "content": "Additional line.",
  "encoding": "utf-8",
  "appendNewLine": true,
  "correlationId": "demo-append-001"
}
```

### 18.5 Replace one exact occurrence

```json
{
  "operation": "REPLACE_TEXT",
  "workspace": "demo",
  "path": "notes\\copilot-demo.txt",
  "searchText": "Microsoft Copilot",
  "replacementText": "Copilot agent",
  "expectedOccurrences": 1,
  "replaceAll": false,
  "caseSensitive": true,
  "useRegex": false,
  "wholeWord": false,
  "correlationId": "demo-replace-001"
}
```

### 18.6 List a directory

```json
{
  "operation": "LIST_DIRECTORY",
  "workspace": "demo",
  "path": "notes",
  "recursive": false,
  "includeFiles": true,
  "includeDirectories": true,
  "includeHidden": false,
  "maxResults": 100,
  "skip": 0,
  "correlationId": "demo-list-001"
}
```

### 18.7 Search names

```json
{
  "operation": "SEARCH_FILES",
  "workspace": "demo",
  "path": ".",
  "searchPattern": "*.txt",
  "namePattern": "*copilot*",
  "fileExtension": ".txt",
  "recursive": true,
  "includeFiles": true,
  "includeDirectories": false,
  "maxDepth": 10,
  "maxResults": 100,
  "skip": 0,
  "correlationId": "demo-search-files-001"
}
```

### 18.8 Search contents

```json
{
  "operation": "SEARCH_CONTENT",
  "workspace": "demo",
  "path": ".",
  "searchText": "Copilot",
  "searchPattern": "*.txt",
  "caseSensitive": false,
  "useRegex": false,
  "wholeWord": true,
  "recursive": true,
  "maxDepth": 10,
  "maxResults": 100,
  "skip": 0,
  "correlationId": "demo-search-content-001"
}
```

### 18.9 Move a file

```json
{
  "operation": "MOVE",
  "workspace": "demo",
  "path": "notes\\copilot-demo.txt",
  "destinationPath": "archive\\copilot-demo.txt",
  "overwrite": false,
  "createParentDirectories": true,
  "correlationId": "demo-move-001"
}
```

### 18.10 Copy a directory

```json
{
  "operation": "COPY",
  "workspace": "demo",
  "path": "archive",
  "destinationPath": "archive-backup",
  "recursive": true,
  "overwrite": false,
  "createParentDirectories": true,
  "correlationId": "demo-copy-001"
}
```

### 18.11 Delete by moving to recycle

```json
{
  "operation": "DELETE_FILE",
  "workspace": "demo",
  "path": "archive\\copilot-demo.txt",
  "reason": "User asked to remove the demonstration file",
  "correlationId": "demo-delete-001"
}
```

Response:

```json
{
  "success": true,
  "status": "COMPLETED",
  "operation": "DELETE_FILE",
  "message": "Item moved to the recycle directory.",
  "exists": false,
  "affectedCount": 1,
  "recycleId": "20b75b0817be485091642d2225bf7795",
  "recyclePath": "D:\\CopilotPOC\\.recycle\\demo\\2026-07-26\\20b75b0817be485091642d2225bf7795\\archive\\copilot-demo.txt"
}
```

For a later production design, avoid returning the physical recycle path; return only `recycleId`.

### 18.12 Restricted PowerShell command

```json
{
  "operation": "EXECUTE_COMMAND",
  "workspace": "demo",
  "shellCommand": "Get-ChildItem",
  "shellArguments": [
    "-LiteralPath",
    ".",
    "-File",
    "-Recurse",
    "-Name"
  ],
  "timeoutSeconds": 20,
  "reason": "Inspect files in the demo workspace",
  "correlationId": "demo-command-001"
}
```

Rejected example:

```json
{
  "operation": "EXECUTE_COMMAND",
  "workspace": "demo",
  "shellCommand": "Remove-Item",
  "shellArguments": [
    "-LiteralPath",
    ".",
    "-Recurse"
  ]
}
```

Expected rejection:

```json
{
  "success": false,
  "status": "REJECTED",
  "errorCode": "COMMAND_DENIED",
  "message": "The command 'Remove-Item' is not allowlisted.",
  "policyAllowed": false,
  "policyRule": "allowlisted-powershell-commands"
}
```

---

## 19. Swagger / OpenAPI 2.0 definition

Save this as `swagger/api-definition.swagger.yaml`.

```yaml
swagger: "2.0"

info:
  title: Copilot Local Filesystem Broker
  version: "1.0.0"
  description: >
    Executes approved filesystem operations and restricted PowerShell
    commands inside configured local Windows workspaces.

host: localhost:8000
basePath: /api/v1

schemes:
  - http

consumes:
  - application/json

produces:
  - application/json

paths:
  /filesystem/execute:
    post:
      operationId: ExecuteWorkspaceFileOperation
      summary: Execute a workspace filesystem operation
      description: >
        Creates, reads, updates, recycles, moves, copies, lists, or searches
        files inside an approved workspace. It can also run one restricted
        allowlisted PowerShell command with explicit arguments. It does not
        accept arbitrary script text.
      x-ms-visibility: important
      parameters:
        - name: body
          in: body
          required: true
          schema:
            $ref: "#/definitions/FileOperationRequest"
      responses:
        "200":
          description: Operation completed
          schema:
            $ref: "#/definitions/FileOperationResponse"
        "400":
          description: Invalid operation fields
          schema:
            $ref: "#/definitions/FileOperationResponse"
        "403":
          description: Rejected by policy
          schema:
            $ref: "#/definitions/FileOperationResponse"
        "404":
          description: Item not found
          schema:
            $ref: "#/definitions/FileOperationResponse"
        "408":
          description: Operation timed out
          schema:
            $ref: "#/definitions/FileOperationResponse"
        "409":
          description: File conflict
          schema:
            $ref: "#/definitions/FileOperationResponse"
        "500":
          description: Internal server error
          schema:
            $ref: "#/definitions/FileOperationResponse"

definitions:
  FileOperationRequest:
    type: object
    required:
      - operation
      - workspace
    additionalProperties: false
    properties:
      operation:
        type: string
        x-ms-summary: Operation
        enum:
          - CREATE_FILE
          - CREATE_DIRECTORY
          - READ_FILE
          - LIST_DIRECTORY
          - UPDATE_FILE
          - APPEND_FILE
          - REPLACE_TEXT
          - DELETE_FILE
          - DELETE_DIRECTORY
          - MOVE
          - COPY
          - SEARCH_FILES
          - SEARCH_CONTENT
          - GET_METADATA
          - EXISTS
          - EXECUTE_COMMAND

      workspace:
        type: string
        x-ms-summary: Workspace
        description: Configured workspace alias, such as demo.
        minLength: 1
        maxLength: 100

      path:
        type: string
        x-ms-summary: Relative path
        description: Relative source path inside the workspace.
        maxLength: 1000

      destinationPath:
        type: string
        x-ms-summary: Destination path
        description: Relative destination path for MOVE or COPY.
        maxLength: 1000

      content:
        type: string
        x-ms-summary: Content
        description: Content for create, update, or append operations.

      encoding:
        type: string
        x-ms-summary: Encoding
        x-ms-visibility: advanced
        default: utf-8
        enum:
          - utf-8
          - utf-8-bom
          - ascii
          - unicode
          - base64

      overwrite:
        type: boolean
        x-ms-summary: Overwrite
        default: false

      appendNewLine:
        type: boolean
        x-ms-summary: Append newline
        x-ms-visibility: advanced
        default: true

      createParentDirectories:
        type: boolean
        x-ms-summary: Create parent directories
        default: false

      recursive:
        type: boolean
        x-ms-summary: Recursive
        default: false

      force:
        type: boolean
        x-ms-summary: Force
        x-ms-visibility: advanced
        default: false

      searchPattern:
        type: string
        x-ms-summary: Search pattern
        description: File glob such as *.txt.
        maxLength: 200

      searchText:
        type: string
        x-ms-summary: Search text

      replacementText:
        type: string
        x-ms-summary: Replacement text

      expectedOccurrences:
        type: integer
        format: int32
        x-ms-summary: Expected occurrences
        x-ms-visibility: advanced
        minimum: 0
        maximum: 10000
        default: 1

      replaceAll:
        type: boolean
        x-ms-summary: Replace all
        x-ms-visibility: advanced
        default: false

      caseSensitive:
        type: boolean
        x-ms-summary: Case sensitive
        x-ms-visibility: advanced
        default: false

      useRegex:
        type: boolean
        x-ms-summary: Use regular expression
        x-ms-visibility: advanced
        default: false

      wholeWord:
        type: boolean
        x-ms-summary: Whole word
        x-ms-visibility: advanced
        default: false

      includeFiles:
        type: boolean
        x-ms-summary: Include files
        x-ms-visibility: advanced
        default: true

      includeDirectories:
        type: boolean
        x-ms-summary: Include directories
        x-ms-visibility: advanced
        default: false

      includeHidden:
        type: boolean
        x-ms-summary: Include hidden items
        x-ms-visibility: advanced
        default: false

      fileExtension:
        type: string
        x-ms-summary: File extension
        x-ms-visibility: advanced
        maxLength: 50

      namePattern:
        type: string
        x-ms-summary: Name pattern
        x-ms-visibility: advanced
        maxLength: 200

      maxDepth:
        type: integer
        format: int32
        x-ms-summary: Maximum depth
        x-ms-visibility: advanced
        minimum: 0
        maximum: 20
        default: 10

      maxResults:
        type: integer
        format: int32
        x-ms-summary: Maximum results
        x-ms-visibility: advanced
        minimum: 1
        maximum: 1000
        default: 100

      skip:
        type: integer
        format: int32
        x-ms-summary: Results to skip
        x-ms-visibility: advanced
        minimum: 0
        default: 0

      maxContentCharacters:
        type: integer
        format: int32
        x-ms-summary: Maximum returned characters
        x-ms-visibility: advanced
        minimum: 0
        maximum: 400000
        default: 100000

      returnContent:
        type: boolean
        x-ms-summary: Return content
        x-ms-visibility: advanced
        default: true

      returnMetadata:
        type: boolean
        x-ms-summary: Return metadata
        x-ms-visibility: advanced
        default: true

      returnHash:
        type: boolean
        x-ms-summary: Return SHA-256 hash
        x-ms-visibility: advanced
        default: false

      expectedHash:
        type: string
        x-ms-summary: Expected SHA-256 hash
        x-ms-visibility: advanced
        maxLength: 100

      expectedLastModifiedUtc:
        type: string
        format: date-time
        x-ms-summary: Expected modification time
        x-ms-visibility: advanced

      shellCommand:
        type: string
        x-ms-summary: PowerShell command
        description: Exact allowlisted command name for EXECUTE_COMMAND.
        maxLength: 100

      shellArguments:
        type: array
        x-ms-summary: PowerShell arguments
        description: Explicit argument array. Script text is not accepted.
        items:
          type: string

      reason:
        type: string
        x-ms-summary: Reason
        maxLength: 1000

      timeoutSeconds:
        type: integer
        format: int32
        x-ms-summary: Timeout in seconds
        x-ms-visibility: advanced
        minimum: 1
        maximum: 60
        default: 20

      correlationId:
        type: string
        x-ms-summary: Correlation ID
        x-ms-visibility: advanced
        maxLength: 200

  FileOperationResponse:
    type: object
    required:
      - success
      - status
      - operation
      - operationId
      - message
      - items
      - matches
    properties:
      success:
        type: boolean

      status:
        type: string
        enum:
          - COMPLETED
          - PARTIAL
          - REJECTED
          - FAILED
          - TIMEOUT
          - CONFLICT
          - NOT_FOUND

      operation:
        type: string

      operationId:
        type: string

      correlationId:
        type: string

      workspace:
        type: string

      path:
        type: string

      destinationPath:
        type: string

      message:
        type: string

      errorCode:
        type: string

      errorMessage:
        type: string

      exists:
        type: boolean

      itemType:
        type: string
        enum:
          - FILE
          - DIRECTORY
          - REPARSE_POINT
          - OTHER

      content:
        type: string

      encoding:
        type: string

      contentTruncated:
        type: boolean

      name:
        type: string

      extension:
        type: string

      sizeBytes:
        type: integer
        format: int64

      createdUtc:
        type: string
        format: date-time

      modifiedUtc:
        type: string
        format: date-time

      hash:
        type: string

      affectedCount:
        type: integer
        format: int32

      totalResults:
        type: integer
        format: int32

      returnedResults:
        type: integer
        format: int32

      hasMore:
        type: boolean

      nextSkip:
        type: integer
        format: int32

      items:
        type: array
        items:
          $ref: "#/definitions/FileSystemItem"

      matches:
        type: array
        items:
          $ref: "#/definitions/ContentMatch"

      exitCode:
        type: integer
        format: int32

      stdout:
        type: string

      stderr:
        type: string

      outputTruncated:
        type: boolean

      recycleId:
        type: string

      recyclePath:
        type: string

      durationMs:
        type: integer
        format: int64

      policyAllowed:
        type: boolean

      policyRule:
        type: string

  FileSystemItem:
    type: object
    properties:
      name:
        type: string
      relativePath:
        type: string
      itemType:
        type: string
        enum:
          - FILE
          - DIRECTORY
          - REPARSE_POINT
          - OTHER
      extension:
        type: string
      sizeBytes:
        type: integer
        format: int64
      createdUtc:
        type: string
        format: date-time
      modifiedUtc:
        type: string
        format: date-time
      isHidden:
        type: boolean
      isReadOnly:
        type: boolean
      hash:
        type: string

  ContentMatch:
    type: object
    properties:
      relativePath:
        type: string
      lineNumber:
        type: integer
        format: int32
      columnNumber:
        type: integer
        format: int32
      matchedText:
        type: string
      lineText:
        type: string
      beforeText:
        type: string
      afterText:
        type: string
```

Update `host` and `schemes` to match the address configured through the gateway. The schema deliberately has no `securityDefinitions` because authentication is excluded from this local proof of concept.

---

## 20. Power Platform custom connector configuration

1. Start the FastAPI server and confirm that `/health` is reachable from the gateway machine.
2. Create or select the standard on-premises data gateway; personal mode is not appropriate for Power Apps or Power Automate.
3. Import `api-definition.swagger.yaml` when creating the custom connector.
4. Configure the connector host and base URL to the address reachable through the gateway.
5. Select **No authentication** for the proof of concept.
6. Verify that the connector exposes one action: `ExecuteWorkspaceFileOperation`.
7. Test `EXISTS`, `CREATE_FILE`, `READ_FILE`, `UPDATE_FILE`, `SEARCH_FILES`, and `DELETE_FILE`.
8. Add the connector action as a tool/action in the Copilot agent.
9. Give the action a precise description:

```text
Execute a controlled local workspace operation. Use structured operations for
file CRUD and search. Use EXECUTE_COMMAND only for an explicitly allowlisted
PowerShell command with separate arguments. All paths are relative to the
selected workspace. Never submit an absolute path or arbitrary script.
```

10. In the agent instructions, state that destructive actions require clear user intent and that structured operations are preferred over command mode.

---

## 21. Windows installation and deployment

### 21.1 Prepare directories

Run from an elevated PowerShell terminal:

```powershell
New-Item -ItemType Directory -Force -Path `
  "D:\CopilotPOC\Workspace", `
  "D:\CopilotPOC\.recycle\demo", `
  "D:\CopilotPOC\Logs", `
  "D:\CopilotPOC\Temp"
```

### 21.2 Create a Python virtual environment

```powershell
cd D:\CopilotPOC\copilot-file-broker
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 21.3 Run locally

### `run.ps1`

```powershell
$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
& ".\.venv\Scripts\python.exe" `
  -m uvicorn app.main:app `
  --host 127.0.0.1 `
  --port 8000
```

Run:

```powershell
.\run.ps1
```

Health test:

```powershell
Invoke-RestMethod -Method Get `
  -Uri "http://127.0.0.1:8000/health"
```

### 21.4 Test the API directly

```powershell
$body = @{
  operation = "CREATE_FILE"
  workspace = "demo"
  path = "notes\hello.txt"
  content = "Hello from Copilot"
  encoding = "utf-8"
  overwrite = $false
  createParentDirectories = $true
  correlationId = "manual-test-001"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/filesystem/execute" `
  -ContentType "application/json" `
  -Body $body
```

### 21.5 Service account and ACLs

Even though authentication is intentionally absent, the server process should not run as an administrator.

For a stronger proof of concept:

1. Create a dedicated local account such as `CopilotFileBroker`.
2. Deny interactive logon.
3. Grant read/write/modify only to:
   - `D:\CopilotPOC\Workspace`
   - `D:\CopilotPOC\.recycle`
   - `D:\CopilotPOC\Logs`
   - `D:\CopilotPOC\Temp`
4. Grant read/execute to the Python environment and application folder.
5. Do not grant access to user profiles, credential stores, SSH directories, browser data, or unrelated drives.
6. Run the service under that account.

The Windows account’s ACLs provide an independent security boundary if application validation fails.

---

## 22. Test plan

### 22.1 Path-security tests

- Reject `C:\Windows\System32`
- Reject `D:relative-drive-path`
- Reject `\\server\share`
- Reject `..\outside.txt`
- Reject `folder\..\outside.txt`
- Reject `file.txt:hidden-stream`
- Reject operations on `.`
- Reject existing reparse points
- Reject a path whose resolved target is outside the workspace
- Permit normal nested relative paths

### 22.2 CRUD tests

- Create a file with parent creation
- Reject duplicate create when `overwrite=false`
- Read UTF-8 content
- Return a SHA-256 hash
- Update with the correct expected hash
- Reject update with an outdated hash
- Append with and without a newline
- Replace exactly one occurrence
- Reject unexpected replacement count
- Create and list a directory
- Move a file
- Copy a file
- Copy a directory only when `recursive=true`
- Recycle a file
- Recycle a directory
- Verify source no longer exists after recycle

### 22.3 Search tests

- Search by glob
- Search by extension
- Search recursively
- Enforce maximum depth
- Enforce maximum results
- Verify pagination with `skip`
- Search content case-insensitively
- Search content with whole-word matching
- Search content with a permitted regular expression
- Skip binary or non-UTF-8 files
- Verify content-match line and column numbers

### 22.4 Restricted-command tests

- Permit every allowlisted command
- Reject non-allowlisted commands
- Reject `pwsh.exe`, `powershell.exe`, and `cmd.exe`
- Reject semicolon, pipe, redirect, ampersand, backtick, and newline
- Reject absolute path arguments
- Reject parent traversal
- Reject environment-variable syntax
- Enforce timeout
- Truncate stdout
- Truncate stderr
- Verify process working directory equals the selected workspace
- Verify a minimal environment is supplied

### 22.5 API tests

- Validate required global fields
- Reject unknown fields
- Return stable response shape
- Return 400 for invalid operation-field combinations
- Return 403 for policy violations
- Return 404 for missing items
- Return 408 for timeout
- Return 409 for conflicts
- Return 500 without stack traces
- Write one audit record per request

---

## 23. Example tests

### `tests/test_security.py`

```python
from pathlib import Path
import pytest

from app.config import Workspace
from app.security import PolicyViolation, resolve_workspace_path


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
        permissions={},
        policy={"allowed_extensions": [".txt"]},
        command_policy={},
    )


@pytest.mark.parametrize(
    "value",
    [
        r"C:\Windows\System32",
        r"D:relative.txt",
        r"\\server\share\file.txt",
        r"..\outside.txt",
        r"folder\..\outside.txt",
        r"file.txt:hidden",
    ],
)
def test_rejects_unsafe_paths(workspace: Workspace, value: str):
    with pytest.raises(PolicyViolation):
        resolve_workspace_path(workspace, value)


def test_accepts_nested_relative_path(workspace: Workspace):
    result = resolve_workspace_path(workspace, r"docs\example.txt")
    assert result == workspace.root / "docs" / "example.txt"
```

### `tests/test_api.py`

```python
from fastapi.testclient import TestClient
from app.main import app


client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_unknown_property_is_rejected():
    response = client.post(
        "/api/v1/filesystem/execute",
        json={
            "operation": "EXISTS",
            "workspace": "demo",
            "path": "example.txt",
            "unexpected": "value",
        },
    )
    assert response.status_code == 422
```

---

## 24. Security review checklist

Before demonstrating the agent:

- [ ] Server binds only to the interface required by the gateway.
- [ ] Windows Firewall permits only the gateway machine or local interface.
- [ ] FastAPI process does not run as administrator.
- [ ] Workspace root and recycle root have restrictive NTFS ACLs.
- [ ] No production files are present in the demo workspace.
- [ ] No secrets are present in files or environment variables.
- [ ] Arbitrary script fields do not exist in the schema.
- [ ] Restricted command allowlist is read-only.
- [ ] Nested shells and executable paths are denied.
- [ ] Absolute paths, UNC paths, traversal, ADS, and reparse points are denied.
- [ ] Response sizes and command output are capped.
- [ ] All operations have a timeout.
- [ ] Deletes move items to the recycle directory.
- [ ] Audit logs exclude file contents and command output.
- [ ] Copilot instructions require explicit user intent for deletion.
- [ ] The Power Platform environment is a non-production environment.
- [ ] Gateway and Python dependencies are current.

---

## 25. Known limitations

1. **No authentication:** Any process that can reach the API can invoke it. Network and Windows ACL isolation are mandatory for the proof of concept.
2. **Single-process concurrency:** Concurrent writes need stronger locking if multiple users or flows operate on the same files.
3. **Search memory use:** The example gathers matches in memory. Large workspaces require streaming internally, indexing, or stronger caps.
4. **Regex denial of service:** Arbitrary regular expressions can consume excessive CPU. Disable `useRegex` initially or use a constrained regex engine and timeout.
5. **PowerShell argument parsing:** PowerShell quoting is complex. Per-command parameter profiles are safer than a generic argument validator.
6. **Recycle retention:** The MVP does not automatically expire recycled items.
7. **No restore operation:** The recycle ID is returned, but restoration is not included in the confirmed operation list.
8. **No binary search:** Content search treats approved files as UTF-8 text.
9. **No transaction across operations:** A sequence of multiple API calls is not atomic.
10. **Connector payload limits:** Large file content and large search results must be capped to avoid connector response failures.
11. **Swagger generated separately:** FastAPI natively exposes OpenAPI 3.x. The Power Platform Swagger 2.0 file is maintained as a separate contract for this MVP.
12. **No arbitrary developer shell:** The restricted command endpoint is not equivalent to Codex or Claude Code shell access and intentionally cannot run arbitrary scripts.

---

## 26. Recommended demonstration sequence

1. Ask Copilot to check whether `notes\demo.txt` exists.
2. Ask Copilot to create it.
3. Ask Copilot to read it and report the SHA-256 hash.
4. Ask Copilot to update it using the returned hash.
5. Ask Copilot to append a new line.
6. Ask Copilot to search the workspace for `.txt` files.
7. Ask Copilot to search file contents for a known phrase.
8. Ask Copilot to run the allowlisted `Get-ChildItem` command.
9. Ask Copilot to move the file to `archive\demo.txt`.
10. Ask Copilot to delete the file.
11. Verify that it appears under the recycle directory.
12. Review the audit log and correlate each operation by `correlationId`.

This demonstrates both structured CRUD and restricted command execution while preserving one connector operation.

---

## 27. Future improvements after the proof of concept

Priority order:

1. Add API-key or Microsoft Entra ID authentication.
2. Bind authorization to individual workspace aliases.
3. Replace generic PowerShell validation with strict per-command profiles.
4. Add a preview-and-confirm workflow for deletion.
5. Add a restore operation using `recycleId`.
6. Remove physical recycle paths from API responses.
7. Run the API as a Windows service.
8. Add file locking and idempotency keys.
9. Add request-rate and concurrency limits.
10. Add structured security events to Windows Event Log or a SIEM.
11. Add TLS between the gateway and API.
12. Add connector environment policies and DLP controls.
13. Generate Swagger 2.0 from a single source-of-truth schema.
14. Add antivirus scanning for newly created binary files if binary support is retained.
15. Add safe code-execution capability only in a disposable sandbox, never in this file broker process.

---

## 28. Acceptance criteria

The MVP is complete when:

- A Copilot agent invokes one custom connector action.
- The action reaches the local FastAPI server through the gateway.
- All structured CRUD and search operations work inside the `demo` workspace.
- A restricted, allowlisted PowerShell command executes synchronously and returns JSON.
- Arbitrary script text cannot be submitted.
- Absolute paths and workspace escapes are rejected.
- Delete operations move targets to the recycle directory.
- Responses conform to the declared fixed schema.
- The Swagger 2.0 definition imports successfully into Power Platform.
- Audit records are written for successful, failed, and rejected requests.
- The process account cannot access unrelated local directories.

---

## 29. Reference documentation

- Microsoft Learn — Create a custom connector from an OpenAPI definition:  
  https://learn.microsoft.com/en-us/connectors/custom-connectors/define-openapi-definition

- Microsoft Learn — On-premises data gateway documentation:  
  https://learn.microsoft.com/en-us/data-integration/gateway/

- Microsoft Learn — What is an on-premises data gateway?:  
  https://learn.microsoft.com/en-us/data-integration/gateway/service-gateway-onprem

- FastAPI — Request Body and Pydantic models:  
  https://fastapi.tiangolo.com/tutorial/body/

- FastAPI — Features:  
  https://fastapi.tiangolo.com/features/

- Microsoft Learn — PowerShell `Start-Process`:  
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/start-process

---

## 30. Final design statement

The proof of concept uses one explicitly modeled Power Platform connector operation backed by a Windows-only FastAPI service. The API provides structured filesystem CRUD and search operations plus a restricted read-only PowerShell command mode. Workspace aliases, canonical path checks, reparse-point rejection, extension and size limits, process timeouts, output caps, NTFS ACLs, recycle-based deletion, and audit logging constrain the local impact. Arbitrary PowerShell script execution is not exposed.
