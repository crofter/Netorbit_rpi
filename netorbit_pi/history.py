"""Status history in SQLite, so tapping a device can show when it came and went.

Home Assistant's recorder did this for the integration. Here every status change of every
hub and device is one row, plus "new device" rows. Old rows are pruned after
``history_days``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
from typing import Any

from .const import EVENT_NEW_DEVICE

_LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    key TEXT NOT NULL,
    name TEXT,
    status TEXT NOT NULL,
    ip TEXT
);
CREATE INDEX IF NOT EXISTS changes_key_at ON changes (key, at);
CREATE INDEX IF NOT EXISTS changes_at ON changes (at);
"""


class History:
    def __init__(self, path: Path | str, keep_days: int = 90) -> None:
        self.keep_days = keep_days
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        # An SD card does not need a sync after every status change.
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._last: dict[str, str] = {
            key: status for key, status in self._db.execute(
                "SELECT key, status FROM changes WHERE id IN (SELECT MAX(id) FROM changes GROUP BY key)"
            )
        }
        self._last_prune: datetime | None = None

    def close(self) -> None:
        self._db.close()

    def record_nodes(self, nodes: dict[str, Any], now: datetime | None = None) -> int:
        """Store every node whose status differs from the last stored one. Returns rows added."""
        now = now or datetime.now(timezone.utc)
        rows = []
        for key, node in nodes.items():
            if self._last.get(key) != node.status:
                rows.append((now.isoformat(), key, node.name, node.status, node.ip))
                self._last[key] = node.status
        if rows:
            self._db.executemany("INSERT INTO changes (at, key, name, status, ip) VALUES (?, ?, ?, ?, ?)", rows)
        self._maybe_prune(now)
        return len(rows)

    def record_event(self, event: str, payload: dict[str, Any], now: datetime | None = None) -> None:
        if event != EVENT_NEW_DEVICE or not payload.get("mac"):
            return
        now = now or datetime.now(timezone.utc)
        self._db.execute(
            "INSERT INTO changes (at, key, name, status, ip) VALUES (?, ?, ?, 'new', ?)",
            (now.isoformat(), payload["mac"], payload.get("name"), payload.get("ip")),
        )

    def _maybe_prune(self, now: datetime) -> None:
        if self._last_prune and now - self._last_prune < timedelta(hours=6):
            return
        self._last_prune = now
        cutoff = (now - timedelta(days=self.keep_days)).isoformat()
        # Keep each node's latest row so its current status survives however old it is.
        self._db.execute(
            "DELETE FROM changes WHERE at < ? AND id NOT IN (SELECT MAX(id) FROM changes GROUP BY key)", (cutoff,)
        )

    def for_key(self, key: str, days: int = 7, now: datetime | None = None) -> dict[str, Any]:
        """Changes in the last ``days`` and the share of that time the node was online."""
        now = now or datetime.now(timezone.utc)
        start = now - timedelta(days=days)
        before = self._db.execute(
            "SELECT at, status FROM changes WHERE key = ? AND at < ? AND status != 'new' ORDER BY at DESC, id DESC LIMIT 1",
            (key, start.isoformat()),
        ).fetchone()
        rows = self._db.execute(
            "SELECT at, name, status, ip FROM changes WHERE key = ? AND at >= ? ORDER BY at, id",
            (key, start.isoformat()),
        ).fetchall()
        changes = [{"at": at, "name": name, "status": status, "ip": ip} for at, name, status, ip in rows]

        # Online share: walk the timeline from the window start (or first record) to now.
        online = total = 0.0
        cur_status = before[1] if before else None
        cur_t = start if before else None
        for c in changes:
            if c["status"] == "new":
                continue
            t = datetime.fromisoformat(c["at"])
            if cur_t is not None and cur_status is not None:
                span = (t - cur_t).total_seconds()
                total += span
                online += span if cur_status == "online" else 0
            cur_t, cur_status = t, c["status"]
        if cur_t is not None and cur_status is not None:
            span = (now - cur_t).total_seconds()
            total += span
            online += span if cur_status == "online" else 0
        return {
            "key": key,
            "days": days,
            "changes": changes,
            "online_share": round(online / total, 4) if total > 0 else None,
        }
