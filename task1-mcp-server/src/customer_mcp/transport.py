"""Closes two JSON-RPC 2.0 compliance gaps in the SDK stdio transport.

1. `stdio_server()` turns an undecodable line into an `Exception` item on the read stream, and
   the SDK runner drops it with a debug log. JSON-RPC 2.0 requires a reply: -32700 for invalid
   JSON, -32600 for JSON that is not a valid request object.
2. A request whose `id` is missing or not a string/integer (`null`, `32.5`, `true`, `{}`)
   parses as a *notification*, because the SDK models ignore unknown fields, and the runner
   never replies. In MCP every notification method starts with `notifications/` and every
   request must carry a string or integer id, so such a message is an Invalid Request (-32600).

This wrapper sends those replies and passes every valid message through unchanged.
"""

import logging
from types import TracebackType
from typing import Any, Protocol, Self

import anyio
from mcp.shared.message import SessionMessage
from mcp_types import INVALID_REQUEST, PARSE_ERROR, ErrorData, JSONRPCError, JSONRPCNotification
from pydantic import ValidationError

logger = logging.getLogger(__name__)

NOTIFICATION_METHOD_PREFIX = "notifications/"
_JSON_SYNTAX_ERROR_TYPES = frozenset({"json_invalid", "json_type"})


class _MessageSource(Protocol):
    async def receive(self) -> SessionMessage | Exception: ...

    async def aclose(self) -> None: ...


class _MessageSink(Protocol):
    async def send(self, item: SessionMessage, /) -> None: ...


def error_reply_for(exc: Exception) -> JSONRPCError:
    if isinstance(exc, ValidationError) and _is_json_syntax_error(exc):
        return JSONRPCError(
            jsonrpc="2.0", id=None, error=ErrorData(code=PARSE_ERROR, message="Parse error")
        )
    return _invalid_request(_recover_request_id(exc))


def is_request_without_valid_id(message: SessionMessage) -> bool:
    inner = message.message
    return isinstance(inner, JSONRPCNotification) and not inner.method.startswith(
        NOTIFICATION_METHOD_PREFIX
    )


def _invalid_request(request_id: int | str | None) -> JSONRPCError:
    return JSONRPCError(
        jsonrpc="2.0",
        id=request_id,
        error=ErrorData(code=INVALID_REQUEST, message="Invalid Request"),
    )


def _is_json_syntax_error(exc: ValidationError) -> bool:
    return any(err["type"] in _JSON_SYNTAX_ERROR_TYPES for err in exc.errors())


def _recover_request_id(exc: Exception) -> int | str | None:
    """Echo the id when the message carried a usable one, as JSON-RPC 2.0 asks."""
    if not isinstance(exc, ValidationError):
        return None
    for err in exc.errors():
        candidate = err.get("input")
        if not isinstance(candidate, dict):
            continue
        request_id = candidate.get("id")
        if isinstance(request_id, str) or (
            isinstance(request_id, int) and not isinstance(request_id, bool)
        ):
            return request_id
    return None


class ProtocolErrorReplyingStream:
    """A read stream that answers malformed messages instead of dropping them."""

    def __init__(self, inner: _MessageSource, replies: _MessageSink) -> None:
        self._inner = inner
        self._replies = replies

    @property
    def last_context(self) -> Any:
        return getattr(self._inner, "last_context", None)

    async def receive(self) -> SessionMessage | Exception:
        while True:
            item = await self._inner.receive()
            if isinstance(item, Exception):
                await self._reject(error_reply_for(item))
            elif is_request_without_valid_id(item):
                await self._reject(_invalid_request(None))
            else:
                return item

    async def _reject(self, reply: JSONRPCError) -> None:
        logger.warning("Rejected malformed message with JSON-RPC error %d", reply.error.code)
        await self._replies.send(SessionMessage(reply))

    async def aclose(self) -> None:
        await self._inner.aclose()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> SessionMessage | Exception:
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        await self.aclose()
        return None
