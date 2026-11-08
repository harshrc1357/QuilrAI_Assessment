"""The gateway's HTTP surface: authenticate, parse, authorize, then forward.

POST carries a JSON-RPC message and goes through every step. GET and DELETE carry no message,
so they are only authenticated before they are forwarded.

Every refusal happens before the downstream server is called. Each decision is logged to
stderr; the log never holds the token.
"""

import logging
from http import HTTPStatus

import httpx2 as httpx
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_gateway.auth import AuthenticationError, Role, role_from_authorization
from mcp_gateway.config import GatewaySettings
from mcp_gateway.jsonrpc import (
    AUTHENTICATION_REQUIRED,
    INVALID_REQUEST,
    GatewayError,
    JsonRpcMessage,
    error_reply,
    parse_message,
)
from mcp_gateway.policy import authorize, check_mirror_headers
from mcp_gateway.proxy import forward, forwarded_headers

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
# Streamable HTTP: GET opens the server-to-client event stream, DELETE ends the session.
BODYLESS_METHODS = ("GET", "DELETE")


def build_app(settings: GatewaySettings, downstream: httpx.AsyncClient) -> Starlette:
    """`downstream` is the HTTP client for the downstream MCP server; the caller owns it."""

    async def handle(request: Request) -> Response:
        try:
            role = role_from_authorization(
                request.headers.get("authorization"), settings.jwt_secret
            )
        except AuthenticationError as exc:
            logger.warning("rejected unauthenticated request: %s", exc)
            return JSONResponse(
                error_reply(None, AUTHENTICATION_REQUIRED, "Authentication required"),
                status_code=HTTPStatus.UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
            )
        if request.method != "POST":
            return await _forward_without_body(request, role, downstream, settings)
        try:
            body = await _read_body(request, settings.max_body_bytes)
            message = parse_message(body)
        except GatewayError as exc:
            logger.warning("rejected role=%s code=%s", role, exc.code)
            return _error_response(exc)
        try:
            headers = _admit(role, message, request.headers)
            return await forward(
                downstream, "POST", settings.downstream_url, body, headers, message.id
            )
        except GatewayError as exc:
            return _error_response(exc)

    return Starlette(routes=[Route(MCP_PATH, handle, methods=["POST", *BODYLESS_METHODS])])


async def _forward_without_body(
    request: Request, role: Role, downstream: httpx.AsyncClient, settings: GatewaySettings
) -> Response:
    """GET opens the server's event stream; DELETE ends the session.

    Neither carries a JSON-RPC message, so there is no method or tool name to authorize: the
    caller only needs a valid token. The same header allow-list applies.
    """
    try:
        headers = forwarded_headers(request.headers, None)
        logger.info("allowed role=%s http_method=%s", role, request.method)
        return await forward(
            downstream, request.method, settings.downstream_url, b"", headers, None
        )
    except GatewayError as exc:
        return _error_response(exc)


async def _read_body(request: Request, limit: int) -> bytes:
    """Reads at most `limit` bytes, so an oversized body cannot exhaust gateway memory."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise GatewayError(
                INVALID_REQUEST,
                "Request body too large",
                http_status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
    return bytes(body)


def _admit(role: Role, message: JsonRpcMessage, client_headers: Headers) -> dict[str, str]:
    """Authorizes the message; returns the exact headers to forward. Logs the decision."""
    try:
        authorize(role, message)
        headers = forwarded_headers(client_headers, message.id)
        check_mirror_headers(message, headers)
    except GatewayError as exc:
        logger.warning(
            "denied role=%s method=%r tool=%r code=%s",
            role,
            message.method,
            message.tool_name,
            exc.code,
        )
        raise
    logger.info("allowed role=%s method=%r tool=%r", role, message.method, message.tool_name)
    return headers


def _error_response(exc: GatewayError) -> JSONResponse:
    return JSONResponse(exc.reply(), status_code=exc.http_status)
