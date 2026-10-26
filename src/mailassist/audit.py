"""Audit trail and draft ledger - metadata only, never mail content.

AuditLog: append-only JSON lines (timestamp, event, ids, hashes, model, outcome).
Messages are identified by a truncated SHA-256 of their Message-ID, so the log
shows *that* something happened to a message without naming it.

DraftLedger: remembers for which messages a draft was saved, to warn before a
second draft for the same message (duplicate processing).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import MessageRef

ALLOWED_FIELDS = frozenset({
    "proposal_id", "message_key", "content_hash", "model", "owner", "reason",
    "category", "injection", "todos", "warnings", "critical", "facts", "verified",
})


def message_key(message_id: str, ref: MessageRef) -> str:
    basis = message_id or f"{ref.folder}/{ref.uidvalidity}/{ref.uid}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _private_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
        if os.name == "posix":
            path.chmod(0o600)


class AuditLog:
    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else None
        if self._path:
            _private_file(self._path)

    def record(self, event: str, **fields: str | int | bool | None) -> None:
        unexpected = set(fields) - ALLOWED_FIELDS
        if unexpected:  # guard against accidentally logging content
            raise ValueError(f"audit fields not allowed: {sorted(unexpected)}")
        if self._path is None:
            return
        entry = {"ts": _now(), "event": event, **{k: v for k, v in fields.items() if v is not None}}
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class DraftLedger:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            _private_file(Path(path))
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS drafts (message_key TEXT NOT NULL, proposal_id TEXT NOT NULL, saved_at TEXT NOT NULL)"
        )
        self._db.commit()

    def last_saved(self, key: str) -> str | None:
        row = self._db.execute(
            "SELECT saved_at FROM drafts WHERE message_key = ? ORDER BY saved_at DESC LIMIT 1", (key,)
        ).fetchone()
        return row[0] if row else None

    def record(self, key: str, proposal_id: str) -> None:
        self._db.execute("INSERT INTO drafts VALUES (?, ?, ?)", (key, proposal_id, _now()))
        self._db.commit()
