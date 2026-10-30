"""What the server has seen of the people trying to connect to it.

Copilot Studio shows a generic error when the connection fails, so the person
importing the server cannot see why. The server can: this keeps one row per kind
of refused request (so internet scanners only bump a counter) and the time of the
last accepted one. /ready and /setup show it. It holds no tokens or user names.
"""
from __future__ import annotations

import threading
import time
from typing import Any


class ConnectionLog:
    def __init__(self) -> None:
        self._refused: dict[str, dict[str, Any]] = {}
        self._accepted = 0
        self._last_accepted = 0.0
        self._url_without_mcp = 0.0
        self._lock = threading.Lock()

    def accepted(self) -> None:
        with self._lock:
            self._accepted += 1
            self._last_accepted = time.time()

    def refused(self, kind: str, message: str, fix: str = "", seen: str = "") -> None:
        with self._lock:
            row = self._refused.setdefault(kind, {"kind": kind, "count": 0})
            row.update(count=row["count"] + 1, last=time.time(), message=message[:300], fix=fix[:400], seen=seen[:200])

    def wrong_url(self) -> None:
        with self._lock:
            self._url_without_mcp = time.time()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            refused = sorted(self._refused.values(), key=lambda r: -r["last"])
            return {
                "requests_accepted": self._accepted,
                "last_accepted_utc": _utc(self._last_accepted),
                "server_url_without_mcp_seen_utc": _utc(self._url_without_mcp),
                "refused": [{**r, "last": _utc(r["last"])} for r in refused],
            }

    def reset(self) -> None:
        with self._lock:
            self._refused.clear()
            self._accepted = 0
            self._last_accepted = self._url_without_mcp = 0.0


def _utc(ts: float) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None


connections = ConnectionLog()
