"""Tool registration. Every tool takes flat, primitive parameters only.

Copilot Studio drops MCP tools whose input schema uses $ref, anyOf/oneOf,
multi-type arrays or nested objects, so every parameter here is a plain
string, integer, number or boolean with a default. JSON payloads travel as
strings (body_json, query_json) and are parsed server side.
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import admin, core, finops


def register_all(mcp: MCPServer) -> None:
    core.register(mcp)
    admin.register(mcp)
    finops.register(mcp)
