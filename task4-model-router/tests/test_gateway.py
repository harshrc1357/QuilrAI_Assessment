"""Seam 2: the gateway HTTP endpoint, in-process, with a primary and a secondary mock provider."""

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import httpx
import pytest

from model_router.app import COMPLETIONS_PATH, build_app
from model_router.config import GatewaySettings, ProviderSettings
from model_router.limiter import RateLimitExceeded, Reservation, TokenRateLimiter

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

TENANT_KEY = "tenant-a-key-0123456789"
OTHER_KEY = "tenant-b-key-0123456789"
LIMIT = 50_000
PRIMARY_TIMEOUT = 0.3  # the real default is 3.0 s; the e2e tests use it
SLOW = 2.0
PRIMARY = ProviderSettings("primary", "http://primary.test/v1/chat/completions", "gpt-main",
                           "sk-primary-secret", PRIMARY_TIMEOUT)  # fmt: skip
SECONDARY = ProviderSettings("secondary", "http://secondary.test/v1/chat/completions",
                             "claude-backup", "sk-secondary-secret", 1.0)  # fmt: skip
MESSAGES = [{"role": "user", "content": "hi"}]  # json.dumps: 35 characters -> 9 tokens


def completion(text: str, total_tokens: int | None = 120) -> httpx.Response:
    body: dict = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
    }
    if total_tokens is not None:
        body["usage"] = {"total_tokens": total_tokens}
    return httpx.Response(200, json=body)


Behavior = Callable[[httpx.Request], Awaitable[httpx.Response]]


def answer(response: httpx.Response) -> Behavior:
    async def behave(request: httpx.Request) -> httpx.Response:
        return response

    return behave


def fail(error: Exception) -> Behavior:
    async def behave(request: httpx.Request) -> httpx.Response:
        raise error

    return behave


def slow(seconds: float = SLOW) -> Behavior:
    async def behave(request: httpx.Request) -> httpx.Response:
        await anyio.sleep(seconds)
        return completion("too late")

    return behave


class SlowBody(httpx.AsyncByteStream):
    """Headers at once, then a body that takes longer than the primary deadline."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'{"choices": ['
        await anyio.sleep(SLOW)
        yield b"]}"


@dataclass
class Providers:
    primary: Behavior = field(default_factory=lambda: answer(completion("from primary")))
    secondary: Behavior = field(default_factory=lambda: answer(completion("from secondary")))
    calls: dict[str, list[httpx.Request]] = field(
        default_factory=lambda: {"primary": [], "secondary": []}
    )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        name = request.url.host.split(".")[0]
        self.calls[name].append(request)
        behavior = self.primary if name == "primary" else self.secondary
        return await behavior(request)

    def sent(self, name: str) -> dict:
        (request,) = self.calls[name]
        return json.loads(request.content)


@dataclass
class Gateway:
    client: httpx.AsyncClient
    providers: Providers

    async def post(self, body: object = None, *, key: str | None = TENANT_KEY) -> httpx.Response:
        payload = (
            {"model": "any", "messages": MESSAGES, "max_tokens": 100} if body is None else body
        )
        content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        headers = {"authorization": f"Bearer {key}"} if key else {}
        return await self.client.post(COMPLETIONS_PATH, content=content, headers=headers)


@pytest.fixture
async def gateway(tmp_path: Path) -> AsyncIterator[Gateway]:
    providers = Providers()
    settings = GatewaySettings(
        tenant_keys=frozenset({TENANT_KEY, OTHER_KEY}),
        primary=PRIMARY,
        secondary=SECONDARY,
        db_path=tmp_path / "router.db",
        tokens_per_minute=LIMIT,
    )
    limiter = TokenRateLimiter(settings.db_path, limit=LIMIT, window_seconds=60)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(providers.handle)) as upstream,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_app(settings, upstream, limiter)),
            base_url="http://gateway.test",
        ) as client,
    ):
        yield Gateway(client, providers)


def assert_clean_error(response: httpx.Response, status: int, code: str) -> None:
    """The standard error payload, with nothing internal in it."""
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    for leak in ("primary.test", "secondary.test", "sk-", "Traceback", "httpx", "Error("):
        assert leak not in response.text


# --- Routing and failover ----------------------------------------------------------------


async def test_the_primary_answers_and_its_reply_is_returned(gateway: Gateway) -> None:
    response = await gateway.post()
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "from primary"
    assert response.headers["x-gateway-provider"] == "primary"
    assert gateway.providers.calls["secondary"] == []


async def test_each_provider_gets_its_own_model_and_key_and_never_the_tenant_key(
    gateway: Gateway,
) -> None:
    gateway.providers.primary = answer(httpx.Response(429))
    await gateway.post()
    for provider in (PRIMARY, SECONDARY):
        (request,) = gateway.providers.calls[provider.name]
        assert request.headers["authorization"] == f"Bearer {provider.api_key}"
        assert gateway.providers.sent(provider.name) == {
            "model": provider.model, "messages": MESSAGES, "max_tokens": 100,
        }  # fmt: skip
        assert TENANT_KEY not in str(request.headers) + request.content.decode()


@pytest.mark.parametrize(
    "primary",
    [
        answer(httpx.Response(429, json={"error": "slow down"})),
        answer(httpx.Response(408)),
        answer(httpx.Response(500, text="Traceback: boom at primary.test")),
        answer(httpx.Response(503)),
        fail(httpx.ConnectError("refused")),
        fail(httpx.ReadError("reset")),
        answer(httpx.Response(200, text="<html>not json</html>")),
        answer(httpx.Response(200, json=["not", "an", "object"])),
    ],
    ids=["429", "408", "500", "503", "connect-error", "read-error", "not-json", "not-object"],
)
async def test_the_secondary_takes_over_when_the_primary_fails(
    gateway: Gateway, primary: Behavior
) -> None:
    gateway.providers.primary = primary
    response = await gateway.post()
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "from secondary"
    assert response.headers["x-gateway-provider"] == "secondary"


@pytest.mark.parametrize("primary", [slow(), lambda _: _slow_body()], ids=["no-reply", "slow-body"])
async def test_the_primary_is_cut_off_at_its_deadline_and_the_secondary_answers(
    gateway: Gateway, primary: Behavior
) -> None:
    gateway.providers.primary = primary
    started = time.monotonic()
    response = await gateway.post()
    elapsed = time.monotonic() - started
    assert response.headers["x-gateway-provider"] == "secondary"
    assert PRIMARY_TIMEOUT <= elapsed < SLOW  # the slow primary is not waited for


async def _slow_body() -> httpx.Response:
    return httpx.Response(200, stream=SlowBody())


@pytest.mark.parametrize("status", [400, 401, 404, 422])
async def test_a_client_error_from_the_primary_is_not_retried(
    gateway: Gateway, status: int
) -> None:
    gateway.providers.primary = answer(httpx.Response(status, text="bad field 'foo' in sk-xyz"))
    response = await gateway.post()
    assert_clean_error(response, 400, "invalid_request")
    assert gateway.providers.calls["secondary"] == []


@pytest.mark.parametrize(
    ("secondary", "status", "code"),
    [
        (answer(httpx.Response(503, text="secondary.test exploded")), 502, "upstream_unavailable"),
        (answer(httpx.Response(429)), 502, "upstream_unavailable"),
        (fail(httpx.ConnectError("refused")), 502, "upstream_unavailable"),
        (answer(httpx.Response(400)), 400, "invalid_request"),
        (slow(), 504, "upstream_timeout"),
    ],
)
async def test_when_both_providers_fail_a_clean_gateway_error_is_returned(
    gateway: Gateway, secondary: Behavior, status: int, code: str
) -> None:
    gateway.providers.primary = answer(httpx.Response(429))
    gateway.providers.secondary = secondary
    response = await gateway.post()
    assert_clean_error(response, status, code)


# --- Rate limiting -----------------------------------------------------------------------


async def test_the_estimate_is_the_prompt_size_plus_max_tokens(gateway: Gateway) -> None:
    gateway.providers.primary = answer(completion("ok", total_tokens=None))
    response = await gateway.post()
    assert response.headers["x-ratelimit-remaining-tokens"] == str(LIMIT - 9 - 100)


async def test_a_tenant_over_its_limit_gets_429_and_no_provider_is_called(gateway: Gateway) -> None:
    gateway.providers.primary = answer(completion("ok", total_tokens=None))
    body = {"messages": MESSAGES, "max_tokens": 30_000}
    assert (await gateway.post(body)).status_code == 200
    response = await gateway.post(body)
    assert_clean_error(response, 429, "rate_limit_exceeded")
    assert 1 <= int(response.headers["retry-after"]) <= 60
    assert len(gateway.providers.calls["primary"]) == 1
    assert (await gateway.post(body, key=OTHER_KEY)).status_code == 200  # other tenant


async def test_the_real_usage_replaces_the_estimate(gateway: Gateway) -> None:
    gateway.providers.primary = answer(completion("ok", total_tokens=500))
    body = {"messages": MESSAGES, "max_tokens": 40_000}
    assert (await gateway.post(body)).status_code == 200
    response = await gateway.post(body)  # fits, because only 500 tokens were really used
    assert response.status_code == 200
    assert response.headers["x-ratelimit-remaining-tokens"] == str(LIMIT - 500 - 500)


async def test_a_failed_request_gives_its_tokens_back(gateway: Gateway) -> None:
    gateway.providers.primary = answer(httpx.Response(503))
    gateway.providers.secondary = answer(httpx.Response(503))
    body = {"messages": MESSAGES, "max_tokens": 40_000}
    assert (await gateway.post(body)).status_code == 502
    gateway.providers.secondary = answer(completion("ok"))
    assert (await gateway.post(body)).status_code == 200


async def test_a_client_that_leaves_during_settle_still_pays_for_the_used_tokens(
    tmp_path: Path,
) -> None:
    class SlowSettleLimiter(TokenRateLimiter):
        async def settle(self, reservation: Reservation, tokens: int) -> None:
            await anyio.sleep(0.2)  # the client leaves during this pause
            await super().settle(reservation, tokens)

    providers = Providers(primary=answer(completion("ok", total_tokens=500)))
    settings = GatewaySettings(frozenset({TENANT_KEY}), PRIMARY, SECONDARY, tmp_path / "r.db")
    limiter = SlowSettleLimiter(settings.db_path, limit=LIMIT, window_seconds=60)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(providers.handle)) as upstream,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_app(settings, upstream, limiter)),
            base_url="http://gateway.test",
        ) as client,
    ):
        with anyio.move_on_after(0.1) as scope:
            await Gateway(client, providers).post()
    assert scope.cancel_called  # the client left while settle was running
    with pytest.raises(RateLimitExceeded):  # the 500 used tokens are still in the window
        await limiter.reserve(TENANT_KEY, LIMIT)


async def test_a_request_larger_than_the_limit_is_refused(gateway: Gateway) -> None:
    response = await gateway.post({"messages": MESSAGES, "max_tokens": LIMIT})
    assert_clean_error(response, 400, "request_too_large")
    assert gateway.providers.calls["primary"] == []


# --- Request checks ----------------------------------------------------------------------


@pytest.mark.parametrize("key", [None, "", "wrong-key-0123456789", TENANT_KEY + "x"])
async def test_an_unknown_tenant_key_gets_401(gateway: Gateway, key: str | None) -> None:
    response = await gateway.post(key=key)
    assert_clean_error(response, 401, "unauthorized")
    assert gateway.providers.calls["primary"] == []


async def test_a_non_bearer_authorization_gets_401(gateway: Gateway) -> None:
    response = await gateway.client.post(
        COMPLETIONS_PATH, json={"messages": MESSAGES}, headers={"authorization": TENANT_KEY}
    )
    assert_clean_error(response, 401, "unauthorized")


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff",
        b"[1, 2]",
        {"model": "x"},
        {"messages": []},
        {"messages": "hi"},
        {"messages": MESSAGES, "max_tokens": 0},
        {"messages": MESSAGES, "max_tokens": -1},
        {"messages": MESSAGES, "max_tokens": "100"},
        {"messages": MESSAGES, "max_tokens": True},
        {"messages": MESSAGES, "stream": True},
    ],
)
async def test_a_bad_request_body_gets_400(gateway: Gateway, body: object) -> None:
    response = await gateway.post(body)
    assert_clean_error(response, 400, "invalid_request")
    assert gateway.providers.calls["primary"] == []


async def test_max_tokens_defaults_when_it_is_absent(gateway: Gateway) -> None:
    gateway.providers.primary = answer(completion("ok", total_tokens=None))
    response = await gateway.post({"messages": MESSAGES})
    assert response.status_code == 200
    assert int(response.headers["x-ratelimit-remaining-tokens"]) < LIMIT - 9


async def test_the_default_max_tokens_is_sent_so_the_provider_cannot_exceed_the_reservation(
    gateway: Gateway,
) -> None:
    assert (await gateway.post({"messages": MESSAGES})).status_code == 200
    assert gateway.providers.sent("primary")["max_tokens"] == 256


# --- Clean errors everywhere -------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "status"), [("GET", COMPLETIONS_PATH, 405),
                                                        ("POST", "/v1/unknown", 404)])  # fmt: skip
async def test_unknown_routes_get_the_standard_error(
    gateway: Gateway, method: str, path: str, status: int
) -> None:
    response = await gateway.client.request(method, path)
    assert_clean_error(response, status, "not_found" if status == 404 else "method_not_allowed")


async def test_an_internal_failure_gets_500_with_no_details(tmp_path: Path, caplog) -> None:
    class BrokenLimiter:
        async def reserve(self, api_key: str, tokens: int) -> None:
            raise RuntimeError("database is locked at C:/secret/router.db")

    settings = GatewaySettings(frozenset({TENANT_KEY}), PRIMARY, SECONDARY, tmp_path / "x.db")
    app = build_app(settings, httpx.AsyncClient(), BrokenLimiter())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test"
    ) as client:
        response = await client.post(
            COMPLETIONS_PATH,
            json={"messages": MESSAGES},
            headers={"authorization": f"Bearer {TENANT_KEY}"},
        )
    assert_clean_error(response, 500, "internal_error")
    assert "secret" not in response.text
    assert "database is locked" in caplog.text  # the detail stays in the server log
