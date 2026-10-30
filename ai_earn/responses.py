"""Shared response envelopes, independent of the command-line presentation."""

from typing import Any


def success(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def failure(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "data": None, "error": {"code": code, "message": message}}
