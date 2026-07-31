# Implementation Plan: Copilot Local Filesystem Broker MVP

## Overview

Build the Windows-focused FastAPI broker described in
`copilot_local_filesystem_broker_mvp_plan.md`. The service exposes one strict
JSON operation endpoint, constrains all filesystem access to configured
workspace aliases, moves deleted items into a controlled recycle root, and
supports a small read-only PowerShell command surface.

The supplied plan is the approved product specification. Its code listings are
treated as sketches: the implementation keeps the public request and response
contract while closing correctness and security gaps identified during review.

## Architecture Decisions

- Keep the single `POST /api/v1/filesystem/execute` contract required by Power
  Platform, with Pydantic rejecting undeclared fields.
- Load and validate configuration once, while allowing `BROKER_CONFIG_PATH` to
  select an isolated configuration for tests and deployments.
- Resolve Windows-style relative paths beneath a configured workspace and
  reject absolute, UNC, drive-qualified, traversal, ADS, hidden, and reparse
  paths according to policy.
- Keep filesystem behavior in focused operation helpers; keep HTTP/error
  translation and audit orchestration at the API boundary.
- Use per-command PowerShell parameter profiles. An allowlisted command name by
  itself is not sufficient to make arbitrary arguments safe.
- Use JSON Lines audit records that contain identifiers and outcomes, never
  content or command output.

## Threat Model

### Trust boundaries

- Untrusted Copilot/connector JSON enters through FastAPI.
- Untrusted YAML enters through the local configuration file.
- Untrusted path, glob, regex, text, and command arguments reach OS-facing code.
- Files already present inside a workspace may be malformed, binary, hidden, or
  replaced with a reparse point between validation and use.

### Assets

- Files outside configured workspace and recycle roots.
- Integrity and availability of files inside an allowed workspace.
- Process environment, credentials, and host operating system.
- File contents and command output, which must not leak into audit logs.

### Principal abuse cases and controls

| Abuse case | Control |
|---|---|
| Escape workspace via absolute/traversal/ADS path | Lexical rejection, canonical containment, reparse checks, ACL guidance |
| Execute arbitrary PowerShell | Exact command and parameter profiles, literal argument quoting, minimal environment, timeout |
| Exfiltrate secrets through logs | Audit field allowlist; never log content/stdout/stderr |
| Exhaust CPU/memory with search or regex | Request/config caps, bounded traversal/results/file size, conservative regex validation |
| Delete permanently or operate on root | Recycle move only; workspace root is never a mutation target |
| Race a file between validation and access | Revalidate targets immediately before use; document ACL/process-isolation residual risk |

## Operational Questions

1. Which operation ran, for which workspace/path, and under which correlation ID?
2. Was it completed, rejected, conflicted, timed out, or failed, and why?
3. How long did it take, and was output truncated?
4. Can an operator diagnose the request without audit logs containing file
   content or command output?

The MVP answers these through one structured audit record per accepted request.
Metrics and distributed tracing are intentionally deferred because this is a
single-process local proof of concept.

## Task List

### Phase 1: Foundation

#### Task 1: Project and contract foundation

**Description:** Add packaging/runtime files, strict Pydantic API models, typed
errors, and validated YAML configuration.

**Acceptance criteria:**

- The request model declares every Swagger field and rejects unknown fields.
- Invalid configuration fails closed with actionable startup errors.
- Tests can construct isolated workspaces without using `D:\CopilotPOC`.

**Verification:**

- Focused model/config tests pass.
- Python modules compile.

**Dependencies:** None

**Files likely touched:** `app/models.py`, `app/config.py`, `app/errors.py`,
`tests/conftest.py`, `tests/test_config.py`

**Estimated scope:** Medium

#### Task 2: Secure path and policy boundary

**Description:** Implement Windows-relative path resolution, containment,
hidden-item, extension, and reparse-point policy checks.

**Acceptance criteria:**

- Absolute, drive-qualified, UNC, traversal, ADS, and workspace-root mutation
  paths are rejected with stable policy codes.
- Normal nested Windows paths resolve correctly.
- Existing hidden/reparse ancestors are rejected according to policy.

**Verification:**

- Focused path-security tests pass.
- Security abuse-case matrix is covered by tests.

**Dependencies:** Task 1

**Files likely touched:** `app/security.py`, `tests/test_security.py`

**Estimated scope:** Small

### Checkpoint: Foundation

- All foundation tests pass.
- Configuration and path resolution fail closed.

### Phase 2: Structured operations

#### Task 3: File CRUD and metadata slice

**Description:** Implement create, read, update, append, replace, metadata, and
existence behavior with encoding, size, and optimistic-concurrency checks.

**Acceptance criteria:**

- Each operation enforces workspace policy and item type.
- Writes are atomic where replacement is expected.
- Hash/timestamp conflicts have stable error codes.

**Verification:**

- Focused CRUD tests pass.
- A create/read/update/append/replace flow succeeds in a temporary workspace.

**Dependencies:** Tasks 1-2

**Files likely touched:** `app/filesystem.py`, `tests/test_filesystem.py`

**Estimated scope:** Medium

#### Task 4: Directory, copy/move/recycle, and search slice

**Description:** Add directory operations, safe move/copy semantics,
recycle-based deletion, bounded name search, and bounded content search.

**Acceptance criteria:**

- Directory copy requires `recursive=true`; directory delete requires explicit
  recursion when non-empty.
- Overwrite behavior replaces only compatible targets and never nests
  unexpectedly.
- Search honors depth, hidden/reparse policy, pagination, policy caps, case,
  whole-word, and regex options.

**Verification:**

- Focused directory/search/recycle tests pass.
- Recycled sources disappear and destinations remain under recycle root.

**Dependencies:** Tasks 1-3

**Files likely touched:** `app/filesystem.py`, `tests/test_filesystem.py`,
`tests/test_search.py`

**Estimated scope:** Medium

#### Task 5: Dispatcher and HTTP API slice

**Description:** Validate operation-specific fields, enforce permissions,
dispatch all structured operations, and return one stable response/error shape.

**Acceptance criteria:**

- Invalid field combinations return 400, policy violations 403, missing items
  404, conflicts 409, and unexpected errors 500 without stack traces.
- Every structured operation is reachable through the single endpoint.
- Response fields remain stable and pagination metadata is correct.

**Verification:**

- Focused API integration tests pass.
- Health and representative CRUD/search flows pass through `TestClient`.

**Dependencies:** Tasks 1-4

**Files likely touched:** `app/dispatcher.py`, `app/main.py`,
`app/response_factory.py`, `tests/test_api.py`

**Estimated scope:** Medium

### Checkpoint: Structured operations

- All structured operation tests pass.
- End-to-end temporary-workspace flow works through HTTP.

### Phase 3: Command, audit, and delivery

#### Task 6: Restricted PowerShell slice

**Description:** Add per-command read-only parameter profiles and a bounded
PowerShell 7 subprocess executor.

**Acceptance criteria:**

- Only the five configured read-only commands and their approved parameter
  shapes are accepted.
- Shell metacharacters, nested shells, executable paths, environment syntax,
  wildcards (unless configured), absolute paths, and traversal are rejected.
- Execution uses the workspace CWD, a minimal environment, timeout, and output
  caps.

**Verification:**

- Validator and mocked subprocess tests pass on any platform.
- A live PowerShell smoke test runs when `pwsh.exe` is available.

**Dependencies:** Tasks 1-2

**Files likely touched:** `app/powershell.py`, `tests/test_commands.py`

**Estimated scope:** Medium

#### Task 7: Audit and complete API integration

**Description:** Write one best-effort structured audit record for successful,
failed, rejected, and validation-error requests without sensitive payloads.

**Acceptance criteria:**

- Accepted endpoint requests produce exactly one record.
- Records contain identifiers/outcome fields and omit content/stdout/stderr.
- Audit write failures are logged but do not replace the operation response.

**Verification:**

- Audit and API error-path tests pass.
- An induced failure is findable by correlation ID in the JSONL output.

**Dependencies:** Tasks 5-6

**Files likely touched:** `app/audit.py`, `app/main.py`, `tests/test_audit.py`,
`tests/test_api.py`

**Estimated scope:** Medium

#### Task 8: Connector and operator deliverables

**Description:** Add Swagger 2.0, example configuration, startup script,
dependency manifests, ignore rules, and operator documentation.

**Acceptance criteria:**

- Swagger is valid YAML, declares the complete fixed schema, and has exactly
  one connector operation.
- README covers setup, configuration, security boundaries, commands, and
  gateway/operator steps.
- Runtime artifacts and secrets are excluded by `.gitignore`.

**Verification:**

- Swagger/config parse tests pass.
- README commands match checked-in files.

**Dependencies:** Tasks 1-7

**Files likely touched:** `swagger/api-definition.swagger.yaml`,
`config/workspaces.yaml`, `README.md`, `run.ps1`, dependency files

**Estimated scope:** Medium

### Checkpoint: Complete

- Full test suite passes with no skips other than an explicitly conditional live
  PowerShell smoke test.
- Python compilation and static checks pass.
- Dependency audit has no unmitigated high/critical findings where tooling is
  available.
- Fresh-context adversarial review has no unresolved critical/required finding.
- The implementation is ready for local gateway integration.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| No authentication in the approved MVP | High | Loopback default, explicit warning, firewall/ACL/service-account instructions |
| Windows path semantics differ on non-Windows test hosts | High | Parse with `PureWindowsPath`; test Windows path forms explicitly |
| PowerShell parameter ambiguity | High | Per-command profiles rather than generic name-only allowlist |
| Reparse-point TOCTOU | High | Revalidate immediately before use plus restrictive process ACLs |
| Regex denial of service | Medium | Conservative pattern checks and bounded file/search sizes |
| Concurrent writes | Medium | Atomic replacement for complete writes; document single-process MVP limitation |
| Audit disk failure | Medium | Best-effort audit with server-side error logging; operation response remains stable |

## Open Questions

- None blocking. Live gateway, firewall, ACL, and Copilot connector provisioning
  require operator access outside this repository and are documented rather
  than automated.
