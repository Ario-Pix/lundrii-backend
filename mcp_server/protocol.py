"""
The MCP wire protocol: JSON-RPC 2.0 over Streamable HTTP.

Implemented directly rather than pulled in as a dependency. A tools-only server
needs five methods and no streaming, which is far less code than the official
SDK brings with it — and that SDK is built on ASGI/Starlette, which this WSGI
project does not otherwise need. The same reasoning that kept Redis and Celery
out applies here.

What a client exchanges with us:

    initialize                -> capabilities + protocol version
    notifications/initialized -> acknowledged with 202, no body
    ping                      -> {}
    tools/list                -> the tools in mcp_server/tools.py
    tools/call                -> run one tool as the authenticated student

Errors split two ways, and the distinction matters to the model. A *protocol*
failure (unknown method, malformed params) is a JSON-RPC error object. A *tool*
failure — slot already taken, quota exhausted — is a successful JSON-RPC
response carrying `isError: true`, so the model reads the reason and can adapt
instead of seeing an opaque transport fault.
"""

from __future__ import annotations

import logging

from base.apidocs import MCP_SERVER_INSTRUCTIONS
from mcp_server import tools as tool_registry
from mcp_server.tools import ToolError

logger = logging.getLogger(__name__)

SERVER_NAME = "lundrii"
SERVER_VERSION = "1.0.0"

# Newest revision we implement, plus the older ones we stay compatible with.
LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_INSTRUCTIONS = MCP_SERVER_INSTRUCTIONS

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _result(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def negotiate_version(requested) -> str:
    """Echo the client's version when we speak it, else offer our newest."""
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return LATEST_PROTOCOL_VERSION


def handle_message(message, student) -> dict | None:
    """
    Handle one JSON-RPC message.

    Returns the response dict, or None for notifications (which by spec get no
    response body at all).
    """
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "Expected a JSON-RPC 2.0 message.")

    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if not isinstance(method, str):
        return None if is_notification else _error(
            request_id, INVALID_REQUEST, "Missing method."
        )

    # Notifications are one-way; the only one we expect is `initialized`.
    if is_notification:
        if method not in ("notifications/initialized", "notifications/cancelled"):
            logger.debug("Ignoring unknown MCP notification %r", method)
        return None

    if method == "initialize":
        return _result(
            request_id,
            {
                "protocolVersion": negotiate_version(params.get("protocolVersion")),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": SERVER_INSTRUCTIONS,
            },
        )

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": tool_registry.tool_descriptors()})

    if method == "tools/call":
        return _handle_tools_call(request_id, params, student)

    return _error(request_id, METHOD_NOT_FOUND, f"Unknown method {method!r}.")


def _text_result(request_id, text: str, *, is_error: bool = False) -> dict:
    return _result(
        request_id,
        {"content": [{"type": "text", "text": text}], "isError": is_error},
    )


def _handle_tools_call(request_id, params: dict, student) -> dict:
    name = params.get("name")
    if not isinstance(name, str):
        return _error(request_id, INVALID_PARAMS, "tools/call requires a tool name.")

    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _error(request_id, INVALID_PARAMS, "`arguments` must be an object.")

    if name not in tool_registry.TOOLS:
        return _error(request_id, METHOD_NOT_FOUND, f"Unknown tool {name!r}.")

    try:
        return _text_result(request_id, tool_registry.call_tool(name, student, arguments))
    except ToolError as exc:
        # Expected, actionable failures: hand the reason to the model.
        return _text_result(request_id, str(exc), is_error=True)
    except Exception:
        # Never leak a traceback to a chat client.
        logger.exception("MCP tool %r failed for student %s", name, student.pk)
        return _text_result(
            request_id,
            "That request failed unexpectedly. Please try again.",
            is_error=True,
        )
