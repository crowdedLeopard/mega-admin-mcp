"""Per-request time budget so every tool call finishes before the MCP client gives up.

Copilot Studio's connector infrastructure cancels a tool call after 120 seconds
("ConnectorTimeoutError"). Honouring a 60 second Retry-After from a throttled
API inside that window is self-defeating: the client has already walked away.

The auth middleware starts a budget (TOOL_TIME_BUDGET_SECONDS, default 100) for
every MCP request. The runbook caps HTTP timeouts and retry waits to what is
left, and fan-out FinOps tools return partial results rather than nothing.
"""
from __future__ import annotations

import contextvars
import time

_deadline: contextvars.ContextVar[float] = contextvars.ContextVar("megamcp_deadline", default=0.0)


def start(seconds: float) -> contextvars.Token:
    """Start a budget for the current context; returns the token for `clear`."""
    return _deadline.set(time.time() + max(5.0, float(seconds)))


def clear(token: contextvars.Token) -> None:
    try:
        _deadline.reset(token)
    except ValueError:
        _deadline.set(0.0)


def remaining() -> float | None:
    """Seconds left, or None when no budget is active (local scripts, unit tests)."""
    d = _deadline.get()
    if not d:
        return None
    return max(0.0, d - time.time())


def can_wait(seconds: float, reserve: float = 8.0) -> bool:
    """True when sleeping `seconds` still leaves `reserve` seconds to finish the call."""
    rem = remaining()
    return True if rem is None else seconds <= rem - reserve


def http_timeout(default: float) -> float:
    """HTTP timeout for the next request: the configured default, shortened to what is left."""
    rem = remaining()
    if rem is None:
        return default
    return max(5.0, min(default, rem - 3.0))


def fanout_timeout(default: float, reserve: float = 12.0) -> float:
    """Timeout for a gather of many calls: leave time to assemble a partial answer."""
    rem = remaining()
    if rem is None:
        return default
    return max(5.0, min(default, rem - reserve))
