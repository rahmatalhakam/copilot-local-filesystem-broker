# ADR-001: Constrained single-operation filesystem broker

## Status

Accepted

## Date

2026-07-31

## Context

Microsoft Copilot must reach a local Windows workspace through a Power Platform
custom connector and an on-premises data gateway. Power Platform needs an
explicit Swagger 2.0 input schema, while local filesystem and shell access have
a high blast radius. The approved MVP excludes authentication and arbitrary
script execution.

## Decision

Expose one synchronous operation endpoint with a fixed superset request schema.
The caller selects a configured workspace alias and a structured operation.
All filesystem paths are workspace-relative and pass lexical, containment,
hidden-item, extension, and reparse-point policy checks.

Keep an optional read-only PowerShell mode, but validate it with exact command
and per-command parameter profiles. Never expose arbitrary script text or
shell-based writes. Move deletes into a configured recycle root and record
sanitized audit events.

## Alternatives considered

### Expose arbitrary PowerShell

Rejected because quoting and allowlisting script text cannot provide a credible
boundary against command chaining, nested processes, environment expansion,
and access outside the intended workspace.

### One REST endpoint per filesystem operation

Rejected for the MVP because the Copilot/Power Platform integration is intended
to expose one stable action. Operation-specific validation remains server-side
and every possible request field is still declared in Swagger.

### Accept physical root paths from the agent

Rejected because it lets untrusted model output select drives, shares, or other
local directories. Logical aliases keep physical paths and authorization policy
under local operator control.

### Permanently delete targets

Rejected because accidental destructive actions are a primary agent-risk case.
Recycle moves are recoverable by an operator even though restore is outside the
MVP API.

## Consequences

- The connector has one predictable action and response shape.
- Adding a workspace is a configuration change, not an API change.
- Structured handlers own all mutations and can apply operation-specific
  concurrency and size checks.
- The Swagger and Pydantic contracts must be kept in sync by tests.
- The command feature is intentionally less flexible than an interactive shell.
- Network isolation, a dedicated process account, Windows Firewall, and NTFS
  ACLs are mandatory compensating controls while authentication is absent.
