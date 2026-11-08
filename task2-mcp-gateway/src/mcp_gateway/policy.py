"""Method-level authorization: which role may send which JSON-RPC message."""

from collections.abc import Mapping
from http import HTTPStatus

from mcp_gateway.auth import Role
from mcp_gateway.jsonrpc import (
    INVALID_PARAMS,
    TOOLS_CALL,
    UNAUTHORIZED_TOOL_CALL,
    GatewayError,
    JsonRpcMessage,
    invalid_request,
)

ADMIN_TOOL_PREFIX = "admin_"
# MCP 2026-07-28 mirrors the method, and the name of a named method, into these HTTP headers.
MCP_METHOD_HEADER = "mcp-method"
MCP_NAME_HEADER = "mcp-name"
NAME_PARAM_BY_METHOD = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}


def authorize(role: Role, message: JsonRpcMessage) -> None:
    """Raises `GatewayError` when `role` may not send `message`.

    Only `tools/call` is gated. Every other method (`tools/list`, `initialize`, notifications)
    and every client response passes through, so the gateway stays transparent to the session.
    """
    if message.method != TOOLS_CALL:
        return
    tool_name = message.tool_name
    if not isinstance(tool_name, str):
        raise _refusal(
            INVALID_PARAMS,
            "Invalid params: params.name must be a string",
            message,
            HTTPStatus.BAD_REQUEST,
        )
    if is_admin_tool(tool_name) and role is not Role.ADMIN:
        raise _refusal(
            UNAUTHORIZED_TOOL_CALL, "Unauthorized Tool Call", message, HTTPStatus.FORBIDDEN
        )


def is_admin_tool(tool_name: str) -> bool:
    # casefold(): fail closed if the downstream server matches tool names without case.
    return tool_name.casefold().startswith(ADMIN_TOOL_PREFIX)


def check_mirror_headers(message: JsonRpcMessage, forwarded: Mapping[str, str]) -> None:
    """Raises `GatewayError` (-32600) when `Mcp-Method` or `Mcp-Name` disagrees with the body.

    The gateway decides from the body. A downstream server may route by these headers, so a
    header that names a different method or tool would run something the gateway never checked.
    `forwarded` must be the exact headers sent downstream. A header that the body gives no value
    for (for example `Mcp-Name` on `tools/list`) must be absent.
    """
    name_param = NAME_PARAM_BY_METHOD.get(message.method or "")
    expected = {
        MCP_METHOD_HEADER: message.method,
        MCP_NAME_HEADER: (message.params or {}).get(name_param) if name_param else None,
    }
    for header, body_value in expected.items():
        if header in forwarded and forwarded[header] != body_value:
            raise invalid_request(message.id, "Mcp-* headers do not match the body")


def _refusal(
    code: int, text: str, message: JsonRpcMessage, notification_status: HTTPStatus
) -> GatewayError:
    # A request gets its JSON-RPC error with HTTP 200, as an MCP server answers it. MCP over
    # HTTP says a refused notification gets an HTTP error status instead.
    status = HTTPStatus.OK if message.id is not None else notification_status
    return GatewayError(code, text, message.id, http_status=status)
