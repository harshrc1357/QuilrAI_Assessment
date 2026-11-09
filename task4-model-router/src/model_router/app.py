"""The gateway endpoint: authenticate the tenant, reserve tokens, route, settle the usage."""

import hmac
import json
import logging
import math
import uuid
from http import HTTPStatus
from typing import Any

import anyio
import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from model_router import errors
from model_router.config import GatewaySettings
from model_router.limiter import (
    RateLimitExceeded,
    RequestTooLarge,
    Reservation,
    TokenRateLimiter,
)
from model_router.router import Failure, ProviderFailure, route

logger = logging.getLogger(__name__)

COMPLETIONS_PATH = "/v1/chat/completions"
WINDOW_SECONDS = 60.0
# Without max_tokens, the reservation assumes this many completion tokens.
DEFAULT_MAX_TOKENS = 256
# The usual rough size of a token for English text. The estimate only has to be close: the
# provider's real usage replaces it after the call.
CHARS_PER_TOKEN = 4
BEARER_PREFIX = "bearer "

FAILURE_ERRORS = {
    Failure.TIMEOUT: errors.upstream_timeout,
    Failure.UNAVAILABLE: errors.upstream_unavailable,
    Failure.REJECTED: errors.upstream_rejected,
}
HTTP_ERROR_CODES = {
    HTTPStatus.NOT_FOUND: "not_found",
    HTTPStatus.METHOD_NOT_ALLOWED: "method_not_allowed",
}


def build_app(
    settings: GatewaySettings, client: httpx.AsyncClient, limiter: TokenRateLimiter
) -> Starlette:
    async def completions(request: Request) -> Response:
        request_id = uuid.uuid4().hex
        try:
            return await _complete(request, request_id)
        except errors.GatewayError as error:
            return error.response(request_id)
        except Exception:
            # The detail goes to the server log only; the client gets the standard payload.
            logger.exception("internal error request_id=%s", request_id)
            return errors.internal_error().response(request_id)

    async def _complete(request: Request, request_id: str) -> Response:
        api_key = _tenant_key(request.headers.get("authorization", ""), settings.tenant_keys)
        body = _parse_body(await request.body())
        estimate = _estimate_tokens(body)
        reservation = await _reserve(limiter, api_key, estimate)
        is_settled = False
        try:
            routed = await route(client, settings.primary, settings.secondary, body)
            used = estimate if routed.total_tokens is None else routed.total_tokens
            # The provider has answered, so the tokens are used. The shield stops a client
            # that leaves now from cancelling the settle and refunding them.
            with anyio.CancelScope(shield=True):
                await limiter.settle(reservation, used)
                is_settled = True
        except ProviderFailure as failure:
            raise FAILURE_ERRORS[failure.kind]() from None
        finally:
            if not is_settled:
                # A failed or cancelled request uses no tokens. The shield lets the refund
                # finish even when the client has left and the request is being cancelled.
                with anyio.CancelScope(shield=True):
                    await limiter.release(reservation)
        logger.info("request_id=%s provider=%s tokens=%s", request_id, routed.provider, used)
        remaining = max(reservation.remaining + estimate - used, 0)
        headers = {
            "x-request-id": request_id,
            "x-gateway-provider": routed.provider,
            "x-ratelimit-remaining-tokens": str(remaining),
        }
        return Response(routed.body, media_type="application/json", headers=headers)

    async def http_error(request: Request, exc: HTTPException) -> Response:
        """Routing errors (unknown path, wrong method) also get the standard payload."""
        code = HTTP_ERROR_CODES.get(exc.status_code, "invalid_request")
        error = errors.GatewayError(exc.status_code, code, exc.detail)
        return error.response(uuid.uuid4().hex)

    return Starlette(
        routes=[Route(COMPLETIONS_PATH, completions, methods=["POST"])],
        exception_handlers={HTTPException: http_error},
    )


def _tenant_key(authorization: str, tenant_keys: frozenset[str]) -> str:
    if not authorization.lower().startswith(BEARER_PREFIX):
        raise errors.unauthorized()
    key = authorization[len(BEARER_PREFIX) :].strip()
    # Compare with every key in constant time, so timing does not show a partial match.
    matches = [hmac.compare_digest(key.encode(), known.encode()) for known in tenant_keys]
    if not key or not any(matches):
        raise errors.unauthorized()
    return key


def _parse_body(raw: bytes) -> dict[str, Any]:
    try:
        body = json.loads(raw)
    except ValueError:
        raise errors.invalid_request("The request body must be JSON") from None
    if not isinstance(body, dict):
        raise errors.invalid_request("The request body must be a JSON object")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise errors.invalid_request("messages must be a non-empty list")
    max_tokens = body.get("max_tokens", DEFAULT_MAX_TOKENS)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise errors.invalid_request("max_tokens must be a positive integer")
    if body.get("stream") is True:
        raise errors.invalid_request("Streaming is not supported by this gateway")
    # The provider always gets max_tokens, so the reservation is an upper bound on the
    # completion. Without it, the provider could write any number of tokens.
    return {**body, "max_tokens": max_tokens}


def _estimate_tokens(body: dict[str, Any]) -> int:
    """The prompt's size in tokens (from its JSON length) plus the completion's maximum."""
    prompt_tokens = math.ceil(len(json.dumps(body["messages"])) / CHARS_PER_TOKEN)
    return prompt_tokens + body["max_tokens"]


async def _reserve(limiter: TokenRateLimiter, api_key: str, tokens: int) -> Reservation:
    try:
        return await limiter.reserve(api_key, tokens)
    except RequestTooLarge:
        raise errors.request_too_large() from None
    except RateLimitExceeded as exc:
        retry_after = max(math.ceil(exc.retry_after_seconds), 1)
        raise errors.GatewayError(
            HTTPStatus.TOO_MANY_REQUESTS,
            "rate_limit_exceeded",
            "The tenant's token limit per minute is used up",
            {"retry-after": str(retry_after)},
        ) from None
