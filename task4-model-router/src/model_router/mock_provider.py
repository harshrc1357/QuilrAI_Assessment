"""A stand-in LLM provider for local runs and the e2e tests.

Run it with `uv run mock-llm-provider`. Environment variables: MOCK_NAME (default "primary"),
MOCK_PORT (default 8101), and MOCK_SLOW_SECONDS (default 5).

It answers every request with a short completion, and reports `max_tokens` as its usage.
To make it fail, put an instruction for it in the last message: `[primary:429]`,
`[primary:500]`, or `[primary:slow]` (the same with `secondary`). Only the provider with that
name obeys, so one request can make the primary fail and the secondary answer.
"""

import os
from http import HTTPStatus

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from model_router.app import COMPLETIONS_PATH, DEFAULT_MAX_TOKENS

DEFAULT_NAME = "primary"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "8101"
DEFAULT_SLOW_SECONDS = "5"


def build_app(name: str, slow_seconds: float) -> Starlette:
    async def completions(request: Request) -> JSONResponse:
        body = await request.json()
        text = str(body["messages"][-1].get("content", ""))
        if f"[{name}:429]" in text:
            return JSONResponse({"error": "rate limited"}, status_code=HTTPStatus.TOO_MANY_REQUESTS)
        if f"[{name}:500]" in text:
            return JSONResponse({"error": "internal"}, status_code=HTTPStatus.INTERNAL_SERVER_ERROR)
        if f"[{name}:slow]" in text:
            await anyio.sleep(slow_seconds)
        reply = {"role": "assistant", "content": f"Hello from the {name} model ({body['model']})"}
        return JSONResponse(
            {
                "id": f"cmpl-{name}",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [{"index": 0, "message": reply, "finish_reason": "stop"}],
                "usage": {"total_tokens": body.get("max_tokens", DEFAULT_MAX_TOKENS)},
            }
        )

    return Starlette(routes=[Route(COMPLETIONS_PATH, completions, methods=["POST"])])


def main() -> None:
    app = build_app(
        os.environ.get("MOCK_NAME", DEFAULT_NAME),
        float(os.environ.get("MOCK_SLOW_SECONDS", DEFAULT_SLOW_SECONDS)),
    )
    port = int(os.environ.get("MOCK_PORT", DEFAULT_PORT))
    try:
        uvicorn.run(app, host=DEFAULT_HOST, port=port, log_level="warning")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
