"""Guard rails for the universal runbook.

- READ_ONLY=true turns the server into a pure observer.
- ALLOW_PATTERNS / DENY_PATTERNS are regexes matched against "METHOD url".
- Writes (POST, PUT, PATCH, DELETE) are previewed unless confirm=true.
- Every call lands in an in-memory audit ring and a JSON log line.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from typing import Any

from ..config import settings

log = logging.getLogger("megamcp.audit")

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# POSTs that are really reads (query / search / report generation requests)
READ_LIKE_POST = re.compile(
    r"(/providers/Microsoft\.CostManagement/(query|forecast)|/providers/Microsoft\.ResourceGraph/resources|"
    r"/\$batch$|/search/query|/getSchedule|/checkMemberGroups|/getMemberGroups|/getByIds|/estimateCosts|"
    r"/validate$|/checkNameAvailability|/listKeys$|/listSecrets$|/exportData|/queries\b|/query\b)",
    re.IGNORECASE,
)


class PolicyDecision(dict):
    @property
    def allowed(self) -> bool:
        return bool(self.get("allowed"))


class Policy:
    def __init__(self) -> None:
        self._allow = [re.compile(p, re.IGNORECASE) for p in settings.allow_patterns]
        self._deny = [re.compile(p, re.IGNORECASE) for p in settings.deny_patterns]

    @staticmethod
    def is_write(method: str, url: str) -> bool:
        m = method.upper()
        if m not in WRITE_METHODS:
            return False
        if m == "POST" and READ_LIKE_POST.search(url.split("?")[0]):
            return False
        return True

    def evaluate(self, method: str, url: str, confirm: bool) -> PolicyDecision:
        subject = f"{method.upper()} {url}"
        for rx in self._deny:
            if rx.search(subject):
                return PolicyDecision(allowed=False, reason=f"blocked by DENY_PATTERNS ({rx.pattern})")
        if self._allow and not any(rx.search(subject) for rx in self._allow):
            return PolicyDecision(allowed=False, reason="not matched by ALLOW_PATTERNS")
        write = self.is_write(method, url)
        if write and settings.read_only:
            return PolicyDecision(allowed=False, reason="server is in READ_ONLY mode; write operations are disabled")
        if write and settings.require_confirm_for_writes and not confirm:
            return PolicyDecision(allowed=False, needs_confirm=True,
                                  reason="write operation requires confirm=true. Review the preview, confirm with the admin, then call again with confirm=true.")
        return PolicyDecision(allowed=True, write=write)

    def describe(self) -> dict[str, Any]:
        return {
            "read_only": settings.read_only,
            "require_confirm_for_writes": settings.require_confirm_for_writes,
            "allow_patterns": settings.allow_patterns,
            "deny_patterns": settings.deny_patterns,
        }


class AuditLog:
    def __init__(self, size: int) -> None:
        self._ring: deque[dict[str, Any]] = deque(maxlen=size)
        self._lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}
        with self._lock:
            self._ring.append(entry)
        try:
            log.info(json.dumps({"audit": entry}, default=str))
        except Exception:  # noqa: BLE001
            pass

    def recent(self, limit: int = 50, user: str = "") -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._ring)
        if user:
            items = [i for i in items if user.lower() in (i.get("user") or "").lower()]
        return items[-limit:][::-1]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._ring)
        total = len(items)
        failures = sum(1 for i in items if (isinstance(i.get("status"), int) and i["status"] >= 400) or i.get("error"))
        writes = sum(1 for i in items if i.get("write"))
        previews = sum(1 for i in items if i.get("status") == "preview")
        blocked = sum(1 for i in items if i.get("status") == "blocked")
        healed = sum(1 for i in items if i.get("healing"))
        return {"recorded": total, "failures": failures, "writes_executed": writes, "write_previews": previews, "blocked": blocked, "self_healed": healed}


policy = Policy()
audit = AuditLog(settings.audit_log_size)
