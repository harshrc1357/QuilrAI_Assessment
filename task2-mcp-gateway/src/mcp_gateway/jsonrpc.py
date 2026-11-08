"""JSON-RPC 2.0 wire format: strict parsing of one message, error codes, and error replies.

The gateway reads the message to make an authorization decision, so the parser refuses any
body that another parser could read differently (duplicate keys, NaN, bad UTF-8).
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

JSONRPC_VERSION = "2.0"
TOOLS_CALL = "tools/call"

# Standard codes (JSON-RPC 2.0 section 5.1).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Server-defined codes (JSON-RPC reserves -32000..-32099 for them).
AUTHENTICATION_REQUIRED = -32000
UNAUTHORIZED_TOOL_CALL = -32001

RequestId = str | int


@dataclass(frozen=True, slots=True)
class JsonRpcMessage:
    """One message from the client.

    - Request: `method` and `id` are set.
    - Notification: `method` is set, `id` is None.
    - Response (the client answers a server request, e.g. sampling): `method` is None.
    """

    id: RequestId | None
    method: str | None
    params: Mapping[str, Any] | None

    @property
    def tool_name(self) -> Any:
        """The raw `params.name` of a `tools/call` (not checked yet); None for other methods."""
        return (self.params or {}).get("name") if self.method == TOOLS_CALL else None


class GatewayError(Exception):
    """A JSON-RPC error that the gateway sends itself, without a call to the downstream server."""

    def __init__(
        self,
        code: int,
        message: str,
        id_: RequestId | None = None,
        http_status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.id = id_
        self.http_status = http_status

    def reply(self) -> dict[str, Any]:
        return error_reply(self.id, self.code, self.message)


def error_reply(id_: RequestId | None, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": id_, "error": {"code": code, "message": message}}


def parse_message(body: bytes) -> JsonRpcMessage:
    """Raises `GatewayError` (-32700 or -32600, HTTP 400) when the body is not one valid message.

    Batches are refused: MCP (2025-06-18) removed JSON-RPC batching, and one HTTP body must
    carry one authorization decision.
    """
    data = _decode(body)
    if not isinstance(data, dict):
        raise invalid_request(None)
    id_ = data.get("id")
    has_valid_id = _is_valid_id(id_)
    if "id" in data and not has_valid_id:
        raise invalid_request(None)
    reply_id = id_ if has_valid_id else None
    if data.get("jsonrpc") != JSONRPC_VERSION:
        raise invalid_request(reply_id)
    if "method" not in data:
        return _parse_response(data, reply_id)
    method = data["method"]
    params = data.get("params")
    if not isinstance(method, str) or (params is not None and not isinstance(params, dict)):
        raise invalid_request(reply_id)
    return JsonRpcMessage(id=reply_id, method=method, params=params)


def invalid_request(id_: RequestId | None, detail: str | None = None) -> GatewayError:
    message = "Invalid Request" if detail is None else f"Invalid Request: {detail}"
    return GatewayError(INVALID_REQUEST, message, id_, http_status=HTTPStatus.BAD_REQUEST)


def _parse_response(data: dict[str, Any], id_: RequestId | None) -> JsonRpcMessage:
    # A response answers a server request, so it needs that request's id and exactly one of
    # `result` / `error`.
    if id_ is None or ("result" in data) == ("error" in data):
        raise invalid_request(id_)
    return JsonRpcMessage(id=id_, method=None, params=None)


def _decode(body: bytes) -> Any:
    try:
        return json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
        )
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors.
        raise GatewayError(PARSE_ERROR, "Parse error", http_status=HTTPStatus.BAD_REQUEST) from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate key in JSON object")
    return result


def _reject_non_finite_number(constant: str) -> float:
    raise ValueError(f"non-standard JSON constant {constant}")


def _is_valid_id(value: Any) -> bool:
    # MCP request ids are strings or integers; `bool` is a subclass of `int`, so exclude it.
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))
