"""A stand-in LLM provider: streams the last user message back in small chat completion chunks.

Run it with `uv run mock-llm-provider`. Environment variables:
MOCK_PROVIDER_PORT (default 8002), MOCK_PROVIDER_API_KEY (if set, required as a Bearer token),
MOCK_CHUNK_CHARS (default 4), and MOCK_CHUNK_DELAY_SECONDS (default 0.05).
"""

import json
import os
from collections.abc import AsyncIterator
from http import HTTPStatus

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from pii_guardrail.app import COMPLETIONS_PATH, EVENT_STREAM

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "8002"
DEFAULT_CHUNK_CHARS = "4"
DEFAULT_CHUNK_DELAY_SECONDS = "0.05"


def _chunk(content: str | None, finish: str | None = None) -> str:
    delta = {} if content is None else {"content": content}
    choice = {"index": 0, "delta": delta, "finish_reason": finish}
    payload = {
        "id": "mock-1",
        "object": "chat.completion.chunk",
        "model": "mock",
        "choices": [choice],
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _stream(text: str, size: int, delay: float) -> AsyncIterator[str]:
    for start in range(0, len(text), size):
        if start:
            await anyio.sleep(delay)
        yield _chunk(text[start : start + size])
    yield _chunk(None, finish="stop")
    yield "data: [DONE]\n\n"


def build_app(api_key: str | None, size: int, delay: float) -> Starlette:
    async def completions(request: Request) -> Response:
        if api_key and request.headers.get("authorization") != f"Bearer {api_key}":
            return JSONResponse(
                {"error": {"message": "bad provider key"}}, status_code=HTTPStatus.UNAUTHORIZED
            )
        body = await request.json()
        user_messages = [m["content"] for m in body["messages"] if m.get("role") == "user"]
        text = user_messages[-1] if user_messages else ""
        return StreamingResponse(_stream(text, size, delay), media_type=EVENT_STREAM)

    return Starlette(routes=[Route(COMPLETIONS_PATH, completions, methods=["POST"])])


def main() -> None:
    app = build_app(
        os.environ.get("MOCK_PROVIDER_API_KEY"),
        int(os.environ.get("MOCK_CHUNK_CHARS", DEFAULT_CHUNK_CHARS)),
        float(os.environ.get("MOCK_CHUNK_DELAY_SECONDS", DEFAULT_CHUNK_DELAY_SECONDS)),
    )
    port = int(os.environ.get("MOCK_PROVIDER_PORT", DEFAULT_PORT))
    try:
        uvicorn.run(app, host=DEFAULT_HOST, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
