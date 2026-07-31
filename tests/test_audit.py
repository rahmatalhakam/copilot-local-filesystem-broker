from __future__ import annotations

import json
from pathlib import Path

from app.audit import append_audit_record


def test_append_audit_record_writes_structured_json_line(tmp_path: Path) -> None:
    append_audit_record(
        tmp_path,
        {
            "event": "filesystem_operation",
            "operationId": "operation-1",
            "correlationId": "correlation-1",
            "success": True,
        },
    )

    paths = list(tmp_path.glob("audit-*.jsonl"))
    assert len(paths) == 1
    lines = paths[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "filesystem_operation"
    assert record["operationId"] == "operation-1"
    assert record["timestampUtc"].endswith("+00:00")


def test_append_audit_record_does_not_mutate_caller_record(tmp_path: Path) -> None:
    record = {"event": "filesystem_operation", "success": False}

    append_audit_record(tmp_path, record)

    assert record == {"event": "filesystem_operation", "success": False}
