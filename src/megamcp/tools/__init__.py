"""Tool registration. Every tool takes flat, primitive parameters only.

Copilot Studio drops MCP tools whose input schema uses $ref, anyOf/oneOf,
multi-type arrays or nested objects, so every parameter here is a plain
string, integer, number or boolean with a default. JSON payloads travel as
strings (body_json, query_json) and are parsed server side.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..access import enrich
from . import admin, core, fanout, finops, inventory, myaccess


def register_all(mcp: MCPServer) -> None:
    core.register(mcp)
    myaccess.register(mcp)
    admin.register(mcp)
    inventory.register(mcp)
    fanout.register(mcp)
    finops.register(mcp)
    _explain_access_everywhere(mcp)


def _explain_access_everywhere(mcp: MCPServer) -> None:
    """Wrap every tool so a result refused for access reasons carries a confirmed, polite explanation (see access.py).

    The SDK validates arguments against the original signature and then calls tool.fn(**arguments),
    so swapping fn for a pass-through wrapper keeps every schema exactly as it was.
    """
    for tool in mcp._tool_manager.list_tools():  # noqa: SLF001 (no public hook for post-processing results)
        original = tool.fn
        if getattr(original, "_megamcp_wrapped", False):
            continue

        def make(name: str, fn):  # noqa: ANN001
            async def wrapped(**kwargs):  # noqa: ANN003
                return await enrich(name, await fn(**kwargs))

            wrapped._megamcp_wrapped = True  # type: ignore[attr-defined]
            wrapped.__name__ = getattr(fn, "__name__", name)
            wrapped.__doc__ = getattr(fn, "__doc__", None)
            return wrapped

        object.__setattr__(tool, "fn", make(tool.name, original))
