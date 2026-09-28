"""Small durable outbox for offline-first student operations.

The classroom is intentionally local and 1:1, so a single JSON file is a better
fit than another database. Writes are atomic and each item carries an idempotency
key understood by the teacher server.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import app_dir

_OUTBOX_LOCK = threading.RLock()


class DurableOutbox:
    MAX_ITEMS = 500

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else app_dir() / "outbox" / "student.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Every UI/background worker instance in this process must share the
        # same lock; per-instance locks still allowed two outbox writers to race.
        self._lock = _OUTBOX_LOCK

    def _read(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        return value if isinstance(value, list) else []

    def _write(self, items: list[dict[str, Any]]) -> None:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def add(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        item = {
            "queue_id": secrets.token_hex(12),
            "kind": kind,
            "payload": payload,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 0,
            "last_error": "",
        }
        with self._lock:
            items = self._read()
            if len(items) >= self.MAX_ITEMS:
                raise RuntimeError("Offline retry queue is full; sync before adding more results")
            items.append(item)
            self._write(items)
        return item

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = self._read()
        if kind:
            return [item for item in items if item.get("kind") == kind]
        return items

    def remove(self, queue_id: str) -> None:
        with self._lock:
            self._write([item for item in self._read() if item.get("queue_id") != queue_id])

    def mark_failed(self, queue_id: str, error: str) -> None:
        with self._lock:
            items = self._read()
            for item in items:
                if item.get("queue_id") == queue_id:
                    item["attempts"] = int(item.get("attempts", 0)) + 1
                    item["last_error"] = error
                    break
            self._write(items)

    def count(self) -> int:
        return len(self.list())
