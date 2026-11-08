"""The gateway endpoint: forward a streaming chat completion request, redact the reply stream."""

import json
import logging
from collections.abc import AsyncIterator
from http import HTTPStatus

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from pii_guardrail.config import GatewaySettings
from pii_guardrail.sse import redact_events

logger = logging.getLogger(__name__)

COMPLETIONS_PATH = "/v1/chat/completions"
EVENT_STREAM = "text/event-stream"


def _error(status: int, message: str, kind: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status)


def _bad_request(message: str) -> JSONResponse:
    return _error(HTTPStatus.BAD_REQUEST, message, "invalid_request_error")


def _provider_error(status: int, message: str) -> JSONResponse:
    return _error(status, message, "provider_error")


def _check_request(body: bytes) -> JSONResponse | None:
    try:
        payload = json.loads(body)
    except ValueError:
        return _bad_request("Request body must be JSON")
    if not isinstance(payload, dict):
        return _bad_request("Request body must be a JSON object")
    if payload.get("stream") is not True:
        return _bad_request('This gateway only streams: set "stream": true')
    return None


def build_app(settings: GatewaySettings, provider: httpx.AsyncClient) -> Starlette:
    headers = {"content-type": "application/json", "accept": EVENT_STREAM}
    if settings.provider_api_key:
        headers["authorization"] = f"Bearer {settings.provider_api_key}"

    async def completions(request: Request) -> Response:
        body = await request.body()
        refusal = _check_request(body)
        if refusal is not None:
            return refusal
        outgoing = provider.build_request(
            "POST", settings.provider_url, content=body, headers=headers
        )
        try:
            reply = await provider.send(outgoing, stream=True)
        except httpx.TimeoutException:
            logger.warning("provider timed out")
            return _provider_error(HTTPStatus.GATEWAY_TIMEOUT, "LLM provider timed out")
        except httpx.TransportError as exc:
            logger.warning("provider unavailable: %s", type(exc).__name__)
            return _provider_error(HTTPStatus.BAD_GATEWAY, "LLM provider unavailable")

        if reply.status_code != HTTPStatus.OK:
            await reply.aclose()  # the body could echo the prompt, so it is never forwarded
            logger.warning("provider returned HTTP %s", reply.status_code)
            return _provider_error(
                reply.status_code, f"LLM provider returned HTTP {reply.status_code}"
            )
        if not reply.headers.get("content-type", "").startswith(EVENT_STREAM):
            await reply.aclose()
            logger.warning("provider reply is not an event stream")
            return _provider_error(HTTPStatus.BAD_GATEWAY, "LLM provider did not stream")

        logger.info("stream started")
        return StreamingResponse(
            _relay(reply), media_type=EVENT_STREAM, headers={"cache-control": "no-cache"}
        )

    return Starlette(routes=[Route(COMPLETIONS_PATH, completions, methods=["POST"])])


async def _relay(reply: httpx.Response) -> AsyncIterator[str]:
    """Stream the redacted events, and close the provider reply even if the client leaves."""
    try:
        async for event in redact_events(reply.aiter_text()):
            yield event
    finally:
        await reply.aclose()
        logger.info("stream ended")
