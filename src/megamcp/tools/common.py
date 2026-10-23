"""Shared helpers for tools."""
from __future__ import annotations

import json
from typing import Any

from mcp.types import ToolAnnotations

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, open_world_hint=True)


def parse_json_arg(value: str, name: str) -> Any:
    """Parse a JSON string argument; empty string means None."""
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(f"{name} must be valid JSON: {e.msg} at position {e.pos}") from e


def err(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def ok(**data: Any) -> dict[str, Any]:
    return {"ok": True, **data}


def body_of(result: dict[str, Any]) -> Any:
    return result.get("body") if isinstance(result, dict) else None
