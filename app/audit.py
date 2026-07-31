from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping


_audit_lock = Lock()


def append_audit_record(
    log_directory: Path,
    record: Mapping[str, Any],
) -> None:
    """Append one sanitized audit event as a UTF-8 JSON line."""

    timestamp = datetime.now(timezone.utc)
    serialized_record = dict(record)
    serialized_record.setdefault("timestampUtc", timestamp.isoformat())

    log_directory.mkdir(parents=True, exist_ok=True)
    audit_path = log_directory / f"audit-{timestamp:%Y-%m-%d}.jsonl"
    line = json.dumps(
        serialized_record,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    with _audit_lock:
        with audit_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.write("\n")
