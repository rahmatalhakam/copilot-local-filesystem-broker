# Copilot Local Filesystem Broker

A Windows-focused proof-of-concept service that lets a Microsoft Copilot agent
perform controlled filesystem operations inside configured local workspaces.
It exposes one synchronous FastAPI endpoint for a Power Platform custom
connector and an optional, tightly restricted read-only PowerShell mode.

> [!WARNING]
> This MVP intentionally has no API authentication. Bind it to loopback or an
> explicitly restricted gateway interface, run it as a non-administrator,
> restrict network access with Windows Firewall, and grant the process account
> access only to the configured workspace, recycle, log, and temp directories.
> Do not place production data or secrets in a demonstration workspace.

## What it supports

- Create files and directories
- Read, replace, update, and append file content
- List directories and inspect metadata
- Move and copy files or directory trees
- Search names and UTF-8 text content with bounded pagination
- Check whether a path exists
- Move deleted items into a controlled recycle directory
- Run one of five configured read-only PowerShell commands with validated,
  explicit arguments
- Write a sanitized JSON Lines audit record for each accepted API request

The service never accepts arbitrary PowerShell script text, absolute filesystem
paths, command chaining, nested shells, or direct permanent deletion.

## Requirements

- Windows
- Python 3.12 or newer
- PowerShell 7 (`pwsh.exe`) for `EXECUTE_COMMAND`
- A standard on-premises data gateway for Power Platform integration

## Quick start

From PowerShell:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Either create the paths in `config/workspaces.yaml`:

```powershell
New-Item -ItemType Directory -Force -Path `
  "D:\CopilotPOC\Workspace", `
  "D:\CopilotPOC\.recycle\demo", `
  "D:\CopilotPOC\Logs", `
  "D:\CopilotPOC\Temp"
```

or copy that file and change its roots to disposable local directories. Select
an alternate configuration before startup:

```powershell
$env:BROKER_CONFIG_PATH = "C:\path\to\workspaces.local.yaml"
```

Start the server:

```powershell
.\run.ps1
```

The launcher reads both the bind host and port from the selected workspace
configuration; it does not override them with hard-coded command-line values.

Check health:

```powershell
Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/health"
```

## Call the API

All operations use:

```http
POST /api/v1/filesystem/execute
Content-Type: application/json
```

Requests are rejected with HTTP `413` and `REQUEST_BODY_TOO_LARGE` when their
body exceeds 16 MiB. The limit is enforced while reading the ASGI body, before
JSON parsing or Pydantic validation, and rejected bodies are never logged.
String fields also have finite schema limits; notably, optimistic-concurrency
hashes must use `sha256:` followed by exactly 64 hexadecimal characters.

Example:

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

Available operation values:

| Category | Operations |
|---|---|
| Create/write | `CREATE_FILE`, `CREATE_DIRECTORY`, `UPDATE_FILE`, `APPEND_FILE`, `REPLACE_TEXT` |
| Read/search | `READ_FILE`, `LIST_DIRECTORY`, `SEARCH_FILES`, `SEARCH_CONTENT`, `GET_METADATA`, `EXISTS` |
| Transfer/delete | `MOVE`, `COPY`, `DELETE_FILE`, `DELETE_DIRECTORY` |
| Command | `EXECUTE_COMMAND` |

See [the MVP plan](copilot_local_filesystem_broker_mvp_plan.md) for complete
request examples and
[the Swagger 2.0 contract](swagger/api-definition.swagger.yaml) for the fixed
connector schema.

## Workspace configuration

The API accepts a logical alias such as `demo`, never a physical root. Each
entry in `config/workspaces.yaml` defines:

- `root` and `recycle_root`
- Per-operation permissions
- Allowed file extensions
- File, write, search-result, and search-depth caps
- Hidden-item and reparse-point policy
- Allowed read-only PowerShell commands and argument limits

Configuration is validated at startup and fails closed. Workspace and recycle
roots must be distinct and must not overlap another configured workspace.
For `EXECUTE_COMMAND`, when `timeoutSeconds` is omitted, the configured
`server.default_timeout_seconds` value is used. An explicit request value is
preserved within the declared 1-to-60-second range.

## Filesystem safety model

Every API path is interpreted as a Windows-style path relative to the selected
workspace. The broker rejects:

- Absolute, drive-qualified, and UNC paths
- `..` traversal and NTFS alternate-data-stream syntax
- Workspace-root mutation
- Existing symbolic links, junctions, and other reparse points when disabled
- Hidden paths when disabled
- File extensions outside the workspace allowlist
- Requests beyond configured size, depth, count, and output limits

Canonical containment checks are necessary but are not the final security
boundary. Run the process under a dedicated Windows account whose NTFS ACLs
deny access outside the approved directories.

## Restricted PowerShell

The initial command surface is read-only:

- `Get-ChildItem`
- `Get-Content`
- `Test-Path`
- `Get-Item`
- `Select-String`

The broker uses per-command parameter profiles. It rejects unknown parameters,
positional ambiguity, shell metacharacters, environment expansion, encoded
commands, nested shells, executable paths, disallowed wildcards, absolute
paths, and traversal. PowerShell runs with:

- `-NoLogo -NoProfile -NonInteractive`
- The workspace as its working directory
- A minimal environment
- A bounded timeout
- Captured and truncated stdout/stderr

Structured operations are the supported path for all writes.

## Audit records

Audit files are written as `audit-YYYY-MM-DD.jsonl` beneath the configured log
directory. Records contain operation and correlation identifiers, workspace,
relative paths, command name/argument count, outcome, policy rule, and
duration. They intentionally omit request/response content and command output.

Audit writing is best-effort: a disk or permission failure is logged by the
server and does not replace an otherwise valid API response. Operators should
monitor the configured log directory and treat audit-write errors as a service
degradation.

## Power Platform connector

1. Start the broker and verify `/health` from the gateway machine.
2. Use a standard on-premises data gateway, not personal mode.
3. Import `swagger/api-definition.swagger.yaml` as a custom connector.
4. Update `host` and `schemes` in the Swagger file for the gateway-reachable
   broker address.
5. Select **No authentication** only for this isolated proof of concept.
6. Confirm the connector exposes one action:
   `ExecuteWorkspaceFileOperation`.
7. Test `EXISTS`, `CREATE_FILE`, `READ_FILE`, `UPDATE_FILE`, `SEARCH_FILES`,
   and `DELETE_FILE`.
8. Add the connector action to the Copilot agent and require explicit user
   intent for destructive operations.

Recommended action description:

```text
Execute a controlled local workspace operation. Use structured operations for
file CRUD and search. Use EXECUTE_COMMAND only for an explicitly allowlisted
PowerShell command with separate arguments. All paths are relative to the
selected workspace. Never submit an absolute path or arbitrary script.
```

## Development commands

```powershell
# Full test suite
python -m pytest

# Coverage (requires requirements-dev.txt)
python -m pytest --cov=app --cov-report=term-missing

# Compile every Python module
python -m compileall -q app tests
```

Tests use temporary workspaces and do not touch `D:\CopilotPOC`.

## Project structure

```text
app/        FastAPI boundary, dispatcher, policy, filesystem, command, audit
config/     Example workspace configuration
swagger/    Power Platform-compatible OpenAPI 2.0 contract
tests/      Unit and API integration tests
tasks/      Implementation plan and completion checklist
logs/       Local audit-log placeholder; generated records are ignored
```

## Known MVP limitations

- No authentication, TLS termination, or application-level rate limiting
- Single-process optimistic concurrency rather than cross-process file locks
- No restore or automatic recycle retention
- UTF-8-only content search
- Conservative regular-expression support with no hard engine-level timeout
- No transaction spanning multiple API requests
- No automatic Swagger generation from the Pydantic models
- Interactive FastAPI documentation and its undeclared OpenAPI route are
  disabled; the reviewed Swagger 2.0 file is the connector contract
- Reparse-point checks cannot eliminate every filesystem time-of-check/time-of-use
  race; restrictive NTFS ACLs remain mandatory

Before production use, add authentication and authorization, TLS, rate and
concurrency controls, stronger command sandboxing or remove command mode,
cross-process locking, retention/restore policy, a pinned dependency lock, and
production monitoring.
