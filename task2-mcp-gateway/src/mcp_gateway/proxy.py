"""HTTP forwarding to the downstream MCP server.

The request body goes downstream byte for byte. The reply streams back as it arrives, so a
Server-Sent Events (SSE) reply from a Streamable HTTP MCP server passes through unchanged.
"""

import logging
from collections.abc import Mapping
from http import HTTPStatus

import httpx2 as httpx
from starlette.background import BackgroundTask
from starlette.datastructures import Headers
from starlette.responses import StreamingResponse

from mcp_gateway.jsonrpc import INTERNAL_ERROR, GatewayError, RequestId, invalid_request

logger = logging.getLogger(__name__)

# An allow-list, not a deny-list: `Authorization` and `Cookie` are credentials for the
# gateway, and a new client header must not reach the downstream server by accident.
# Every `Mcp-*` header is MCP transport state (session id, protocol version, and the
# 2026-07-28 `Mcp-Method` / `Mcp-Name` / `Mcp-Param-*` mirrors of the body).
FORWARDED_REQUEST_HEADERS = frozenset({"content-type", "accept", "last-event-id"})
MCP_HEADER_PREFIX = "mcp-"
EVENT_STREAM_METHOD = "GET"
# RFC 9110 section 7.6.1: these describe one connection, so a proxy must not pass them on.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
# httpx decodes gzip/deflate while it streams, so the original length and encoding are wrong
# for the bytes the client receives. Starlette sets the transfer framing itself.
DECODED_BODY_HEADERS = frozenset({"content-encoding", "content-length"})
DROPPED_RESPONSE_HEADERS = HOP_BY_HOP_HEADERS | DECODED_BODY_HEADERS


def forwarded_headers(client_headers: Headers, request_id: RequestId | None) -> dict[str, str]:
    """The exact request headers to send downstream.

    Raises `GatewayError` (-32600) for a repeated `Mcp-*` header: one value must be both the
    value the gateway checks and the value the downstream server reads. Other repeated headers
    are joined with ", ", which RFC 9110 section 5.3 makes equal to the repeated form.
    """
    headers: dict[str, str] = {}
    for name in dict.fromkeys(client_headers.keys()):  # Lowercase names, first-seen order.
        is_mcp_header = name.startswith(MCP_HEADER_PREFIX)
        if not is_mcp_header and name not in FORWARDED_REQUEST_HEADERS:
            continue
        values = client_headers.getlist(name)
        if is_mcp_header and len(values) > 1:
            raise invalid_request(request_id, f"repeated {name.title()} header")
        headers[name] = ", ".join(values)
    return headers


async def forward(
    client: httpx.AsyncClient,
    http_method: str,
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    request_id: RequestId | None,
) -> StreamingResponse:
    """Raises `GatewayError` (-32603, HTTP 502 or 504) when the downstream server fails."""
    timeout = client.timeout
    if http_method == EVENT_STREAM_METHOD:
        # The server-to-client event stream may stay quiet for a long time. Only connecting
        # may time out; a read timeout would cut the stream and force a reconnect.
        timeout = httpx.Timeout(timeout.connect, read=None)
    outbound = client.build_request(
        http_method, url, content=body, headers=headers, timeout=timeout
    )
    try:
        reply = await client.send(outbound, stream=True)
    except httpx.TimeoutException as exc:
        logger.warning("downstream timed out: %s", type(exc).__name__)
        raise GatewayError(
            INTERNAL_ERROR,
            "Downstream MCP server timed out",
            request_id,
            HTTPStatus.GATEWAY_TIMEOUT,
        ) from exc
    except httpx.TransportError as exc:
        logger.warning("downstream unavailable: %s", type(exc).__name__)
        raise GatewayError(
            INTERNAL_ERROR, "Downstream MCP server unavailable", request_id, HTTPStatus.BAD_GATEWAY
        ) from exc
    response = StreamingResponse(
        reply.aiter_bytes(),
        status_code=reply.status_code,
        background=BackgroundTask(reply.aclose),
    )
    # multi_items() keeps repeated headers (e.g. two Set-Cookie) apart; a dict would merge them.
    response.raw_headers.extend(
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in reply.headers.multi_items()
        if name.lower() not in DROPPED_RESPONSE_HEADERS
    )
    return response
