"""Seam 2: the gateway HTTP endpoint, in-process, with a mock LLM provider."""

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import httpx
import pytest

from pii_guardrail.app import COMPLETIONS_PATH, build_app
from pii_guardrail.config import GatewaySettings
from pii_guardrail.sse import MAX_LINE_CHARS

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

PROVIDER_URL = "http://provider.test/v1/chat/completions"
PROVIDER_KEY = "sk-provider-test-key"
REQUEST = {"model": "mock-1", "stream": True, "messages": [{"role": "user", "content": "hi"}]}


def chunk_event(*deltas: str | None, finish: str | None = None, index: int = 0) -> str:
    choices = [
        {"index": index, "delta": {} if d is None else {"content": d}, "finish_reason": finish}
        for d in deltas
    ]
    payload = {
        "id": "c-1",
        "object": "chat.completion.chunk",
        "model": "mock-1",
        "choices": choices,
    }
    return f"data: {json.dumps(payload)}\n\n"


def sse(*contents: str, done: bool = True) -> list[str]:
    events = [chunk_event(content) for content in contents]
    return [*events, chunk_event(None, finish="stop")] + (["data: [DONE]\n\n"] if done else [])


class ByteStream(httpx.AsyncByteStream):
    """A provider body sent in the given byte pieces, then an optional transport failure."""

    def __init__(self, pieces: list[bytes], error: Exception | None = None) -> None:
        self._pieces = pieces
        self._error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for piece in self._pieces:
            yield piece
        if self._error is not None:
            raise self._error


@dataclass
class MockProvider:
    """Records each request, then answers with `reply`."""

    reply: Callable[[httpx.Request], httpx.Response]
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.reply(request)


def streaming(events: list[str], *, error: Exception | None = None, split: int = 0) -> MockProvider:
    raw = "".join(events).encode()
    pieces = [raw[i : i + split] for i in range(0, len(raw), split)] if split else [raw]
    return MockProvider(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ByteStream(pieces, error),
        )
    )


def failing(error: Exception) -> MockProvider:
    def reply(request: httpx.Request) -> httpx.Response:
        raise error

    return MockProvider(reply)


@pytest.fixture
async def call() -> AsyncIterator[Callable[..., object]]:
    clients: list[httpx.AsyncClient] = []

    async def post(provider: MockProvider, body: object = REQUEST, **kwargs) -> httpx.Response:
        settings = GatewaySettings(provider_url=PROVIDER_URL, provider_api_key=PROVIDER_KEY)
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(provider.handle))
        gateway = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_app(settings, upstream)),
            base_url="http://gateway.test",
        )
        clients.extend([upstream, gateway])
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return await gateway.post(COMPLETIONS_PATH, content=content, **kwargs)

    yield post
    for client in clients:
        await client.aclose()


def contents(response: httpx.Response) -> str:
    """Join every delta.content in the gateway's SSE reply."""
    text = []
    for line in response.text.splitlines():
        if line.startswith("data: {"):
            for choice in json.loads(line.removeprefix("data: ")).get("choices", []):
                text.append(choice["delta"].get("content", ""))
    return "".join(text)


def data_lines(response: httpx.Response) -> list[str]:
    return [line for line in response.text.splitlines() if line]


# --- Redaction in the stream -------------------------------------------------------------


async def test_pii_split_across_deltas_is_redacted(call) -> None:
    provider = streaming(sse("Mail jane.d", "oe@exam", "ple.com, SSN 123-", "45-6789, card 4111 ",
                             "1111 1111 1111 ok"))  # fmt: skip
    response = await call(provider)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert contents(response) == "Mail [REDACTED], SSN [REDACTED], card [REDACTED] ok"
    assert "jane" not in response.text and "6789" not in response.text
    assert data_lines(response)[-1] == "data: [DONE]"


async def test_every_event_is_valid_sse_and_keeps_its_other_fields(call) -> None:
    response = await call(streaming(sse("Hello ", "a@b.io")))
    events = [
        json.loads(line.removeprefix("data: "))
        for line in data_lines(response)
        if line != "data: [DONE]"
    ]
    assert [e["id"] for e in events] == ["c-1"] * 3
    assert [e["model"] for e in events] == ["mock-1"] * 3
    assert [e["choices"][0]["finish_reason"] for e in events] == [None, None, "stop"]
    assert response.text.count("\n\n") == len(events) + 1


async def test_text_without_pii_passes_through_unchanged(call) -> None:
    response = await call(streaming(sse("Hello, ", "how are ", "you? Café ☕")))
    assert contents(response) == "Hello, how are you? Café ☕"


async def test_sse_split_at_any_byte_is_reassembled(call) -> None:
    events = sse("Café ☕ — mail ", "zoë@exa", "mple.org now")
    for split in (1, 2, 3, 7):
        response = await call(streaming(events, split=split))
        assert contents(response) == "Café ☕ — mail [REDACTED] now", split


async def test_held_tail_is_sent_with_the_finish_event(call) -> None:
    response = await call(streaming(sse("Total due 4111 11")))
    last_chunk = json.loads(data_lines(response)[-2][6:])
    assert last_chunk["choices"][0] == {
        "index": 0, "delta": {"content": "4111 11"}, "finish_reason": "stop",
    }  # fmt: skip


async def test_held_tail_is_sent_before_done_without_a_finish_event(call) -> None:
    events = [chunk_event("Bye "), chunk_event("x@y.io"), "data: [DONE]\n\n"]
    response = await call(streaming(events))
    assert contents(response) == "Bye [REDACTED]"
    assert data_lines(response)[-1] == "data: [DONE]"
    tail = json.loads(data_lines(response)[-2].removeprefix("data: "))
    assert (tail["id"], tail["object"], tail["model"]) == ("c-1", "chat.completion.chunk", "mock-1")


async def test_held_tail_is_sent_when_the_stream_ends_cleanly_without_done(call) -> None:
    response = await call(streaming([chunk_event("Bye "), chunk_event("x@y.io")]))
    assert contents(response) == "Bye [REDACTED]"


async def test_each_choice_has_its_own_redaction_state(call) -> None:
    events = [
        chunk_event("Mail ann@", index=0),
        chunk_event("Mail bo", index=1),
        chunk_event("x.io ok", index=0),
        chunk_event("b@y.io ok", index=1),
        chunk_event(None, finish="stop", index=0),
        chunk_event(None, finish="stop", index=1),
        "data: [DONE]\n\n",
    ]
    response = await call(streaming(events))
    by_choice: dict[int, str] = {0: "", 1: ""}
    for line in data_lines(response)[:-1]:
        for choice in json.loads(line.removeprefix("data: "))["choices"]:
            by_choice[choice["index"]] += choice["delta"].get("content", "")
    assert by_choice == {0: "Mail [REDACTED] ok", 1: "Mail [REDACTED] ok"}


async def test_non_data_lines_and_malformed_events_are_dropped(call) -> None:
    events = [
        ": keep-alive\n\n",
        "event: message\n",
        chunk_event("Hi "),
        "data: not json jane@example.com\n\n",
        'data: ["a list"]\n\n',
        'data: {"error": {"message": "prompt: jane@example.com"}}\n\n',
        'data: {"choices": "jane@example.com"}\n\n',
        'data: {"choices": ["jane@example.com"]}\n\n',
        'data: {"choices": [{"index": [0], "delta": {"content": "jane@example.com"}}]}\n\n',
        chunk_event("there"),
        "data: [DONE]\n\n",
    ]
    response = await call(streaming(events))
    assert contents(response) == "Hi there"
    assert "jane" not in response.text
    assert "keep-alive" not in response.text and "a list" not in response.text


def raw_event(choice: dict) -> str:
    return f"data: {json.dumps({'id': 'c-1', 'choices': [choice]})}\n\n"


async def test_logprobs_are_removed_because_they_repeat_every_token(call) -> None:
    logprob = {"token": "jane@example.com", "logprob": -0.1, "top_logprobs": []}
    choice = {"index": 0, "delta": {"content": "Hi "}, "logprobs": {"content": [logprob]}}
    response = await call(streaming([raw_event(choice), "data: [DONE]\n\n"]))
    assert contents(response) == "Hi "
    assert "jane" not in response.text and "logprobs" not in response.text


async def test_refusal_text_is_redacted_with_its_own_state(call) -> None:
    def refusal_event(text: str) -> str:
        return raw_event({"index": 0, "delta": {"refusal": text}, "finish_reason": None})

    events = [
        chunk_event("Mail "),
        refusal_event("I can't share jane@ex"),
        chunk_event("now"),
        refusal_event("ample.com, sorry"),
        chunk_event(None, finish="stop"),
        "data: [DONE]\n\n",
    ]
    response = await call(streaming(events))
    refusal = "".join(
        choice["delta"].get("refusal", "")
        for line in data_lines(response)[:-1]
        for choice in json.loads(line.removeprefix("data: "))["choices"]
    )
    assert refusal == "I can't share [REDACTED], sorry"
    assert contents(response) == "Mail now"
    assert "jane" not in response.text


async def test_a_line_longer_than_the_limit_ends_the_stream_with_an_error(call) -> None:
    endless = "data: " + "x" * (MAX_LINE_CHARS + 1)  # no newline: the line never ends
    response = await call(streaming([chunk_event("Contact jane@exa"), endless]))
    assert contents(response) == "Contact "
    assert "jane" not in response.text and "xxxx" not in response.text
    last = json.loads(data_lines(response)[-1].removeprefix("data: "))
    assert last["error"]["type"] == "provider_error"


async def test_lines_ended_by_cr_lf_or_cr_are_read(call) -> None:
    events = [event.replace("\n", "\r\n") for event in sse("Mail a@b.io", " ok")]
    for split in (1, 5):
        response = await call(streaming(events, split=split))
        assert contents(response) == "Mail [REDACTED] ok"
    events = [event.replace("\n", "\r") for event in sse("Hi ", "there")]
    assert contents(await call(streaming(events))) == "Hi there"


async def test_a_last_line_without_a_newline_is_read(call) -> None:
    response = await call(streaming([chunk_event("Bye a@b.io"), "data: [DONE]"]))
    assert contents(response) == "Bye [REDACTED]"
    assert data_lines(response)[-1] == "data: [DONE]"


# --- Mid-stream failure ------------------------------------------------------------------


@pytest.mark.parametrize(
    "error", [httpx.ReadError("reset"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("x")]
)
async def test_mid_stream_failure_drops_the_held_tail_and_sends_an_error_event(
    call, error: Exception
) -> None:
    response = await call(streaming([chunk_event("Contact jane@exa")], error=error))
    assert response.status_code == 200
    assert contents(response) == "Contact "
    assert "jane" not in response.text
    last = json.loads(data_lines(response)[-1][6:])
    assert last == {"error": {"message": "LLM provider stream failed", "type": "provider_error"}}


# --- Request forwarding ------------------------------------------------------------------


async def test_request_is_forwarded_with_the_provider_key_only(call) -> None:
    provider = streaming(sse("ok"))
    body = json.dumps(REQUEST).encode()
    await call(provider, body, headers={"Authorization": "Bearer client-token", "Cookie": "s=1"})
    (sent,) = provider.requests
    assert str(sent.url) == PROVIDER_URL
    assert sent.method == "POST"
    assert sent.content == body
    assert sent.headers["authorization"] == f"Bearer {PROVIDER_KEY}"
    assert sent.headers["accept"] == "text/event-stream"
    assert "cookie" not in sent.headers


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b'["a list"]',
        json.dumps({**REQUEST, "stream": False}).encode(),
        json.dumps({k: v for k, v in REQUEST.items() if k != "stream"}).encode(),
        json.dumps({**REQUEST, "stream": "true"}).encode(),
    ],
)
async def test_bad_request_gets_400_and_the_provider_is_not_called(call, body: bytes) -> None:
    provider = streaming(sse("never"))
    response = await call(provider, body)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert provider.requests == []


# --- Provider errors before the stream starts --------------------------------------------


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (httpx.ConnectError("refused"), 502),
        (httpx.ConnectTimeout("slow"), 504),
        (httpx.ReadTimeout("slow"), 504),
    ],
)
async def test_provider_unreachable_gets_a_clean_gateway_error(
    call, error: Exception, status: int
) -> None:
    response = await call(failing(error))
    assert response.status_code == status
    assert response.json()["error"]["type"] == "provider_error"
    assert "provider.test" not in response.text


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
async def test_provider_error_status_is_kept_but_its_body_is_not_forwarded(
    call, status: int
) -> None:
    leaky = '{"error": {"message": "prompt was: my SSN is 123-45-6789"}}'
    provider = MockProvider(lambda _: httpx.Response(status, text=leaky))
    response = await call(provider)
    assert response.status_code == status
    assert response.json() == {
        "error": {"message": f"LLM provider returned HTTP {status}", "type": "provider_error"}
    }


async def test_provider_reply_that_is_not_a_stream_gets_502(call) -> None:
    provider = MockProvider(
        lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "a@b.io"}}]})
    )
    response = await call(provider)
    assert response.status_code == 502
    assert "a@b.io" not in response.text
