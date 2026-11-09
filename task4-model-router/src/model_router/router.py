"""Send a completion request to the primary provider, and fail over to the secondary one."""

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from typing import Any

import anyio
import httpx

from model_router.config import ProviderSettings

logger = logging.getLogger(__name__)

# HTTP 4xx: the client's request is bad.
CLIENT_ERRORS = range(HTTPStatus.BAD_REQUEST, HTTPStatus.INTERNAL_SERVER_ERROR)
# The 4xx codes that say the provider cannot serve now, not that the request is bad.
RETRYABLE_CLIENT_ERRORS = frozenset({HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS})


class Failure(StrEnum):
    TIMEOUT = "timeout"  # no full reply before the provider's deadline
    UNAVAILABLE = "unavailable"  # 408, 429, 5xx, connection failure, or an unreadable reply
    REJECTED = "rejected"  # a 4xx other than 408 or 429: the request itself is bad


class ProviderFailure(Exception):
    def __init__(self, kind: Failure) -> None:
        super().__init__(kind.value)
        self.kind = kind


@dataclass(frozen=True)
class Routed:
    provider: str
    body: bytes
    total_tokens: int | None  # the provider's reported usage, if it sent a valid one


async def route(
    client: httpx.AsyncClient,
    primary: ProviderSettings,
    secondary: ProviderSettings,
    request: dict[str, Any],
) -> Routed:
    """Return the first good reply. A REJECTED primary is not retried: the secondary would
    reject the same request. Raise ProviderFailure with the secondary's failure otherwise."""
    try:
        return await _call(client, primary, request)
    except ProviderFailure as failure:
        if failure.kind is Failure.REJECTED:
            raise
        logger.warning("primary provider failed (%s); failing over", failure.kind)
    return await _call(client, secondary, request)


async def _call(
    client: httpx.AsyncClient, provider: ProviderSettings, request: dict[str, Any]
) -> Routed:
    content = json.dumps({**request, "model": provider.model}).encode()
    headers = {"content-type": "application/json"}
    if provider.api_key:
        headers["authorization"] = f"Bearer {provider.api_key}"
    try:
        # One deadline for the whole exchange, body included. httpx's own timeouts apply
        # per network read, so a slow trickle of bytes could pass them. When the deadline
        # passes, the request is cancelled; a reply that arrives later is never used.
        with anyio.fail_after(provider.timeout_seconds):
            reply = await client.post(provider.url, content=content, headers=headers)
    except (TimeoutError, httpx.TimeoutException):
        raise ProviderFailure(Failure.TIMEOUT) from None
    except httpx.HTTPError as exc:
        logger.warning("%s provider: %s", provider.name, type(exc).__name__)
        raise ProviderFailure(Failure.UNAVAILABLE) from None
    return _read_reply(provider, reply)


def _read_reply(provider: ProviderSettings, reply: httpx.Response) -> Routed:
    status = reply.status_code
    if status != HTTPStatus.OK:
        logger.warning("%s provider returned HTTP %s", provider.name, status)
        is_client_error = status in CLIENT_ERRORS and status not in RETRYABLE_CLIENT_ERRORS
        raise ProviderFailure(Failure.REJECTED if is_client_error else Failure.UNAVAILABLE)
    try:
        payload = reply.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        logger.warning("%s provider sent a reply that is not a JSON object", provider.name)
        raise ProviderFailure(Failure.UNAVAILABLE)
    return Routed(provider.name, reply.content, _total_tokens(payload))


def _total_tokens(payload: dict[str, Any]) -> int | None:
    usage = payload.get("usage")
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    is_count = isinstance(total, int) and not isinstance(total, bool) and total >= 0
    return total if is_count else None
