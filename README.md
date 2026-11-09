# QuilrAI Solution Architect Assessment

My solutions to the four tasks of the take-home. Each task is a separate Python project in
its own folder, with its own README, tests, and lock file.

| Task | Folder | What it builds | Tests |
|---|---|---|---|
| 1 | [`task1-mcp-server`](task1-mcp-server) | An MCP server over stdio, with strict Pydantic validation and JSON-RPC 2.0 errors | 120, ~98% coverage |
| 2 | [`task2-mcp-gateway`](task2-mcp-gateway) | An MCP security gateway that filters tools by JWT role | 93, 99% coverage |
| 3 | [`task3-pii-guardrail`](task3-pii-guardrail) | An LLM gateway that redacts PII in streamed replies | 80, 100% coverage |
| 4 | [`task4-model-router`](task4-model-router) | An LLM router with a token rate limiter in SQLite and failover to a second provider | 79, 100% coverage |

## How to read a task

Open the task's README. Its **Reading guide** lists the files that answer the task, in
order. The rest of the README explains the design, the tests, and the decisions.

## How to run a task

Prerequisites: Python 3.11+ and [uv](https://docs.astral.sh/uv/). From the task's folder:

```bash
uv sync
```

```bash
uv run pytest --cov
```

Each task README shows how to start its servers and try a request.

## How I built it

Test-first, one small slice at a time. Every task passes `ruff` and `bandit`.
Tested on Windows 11 with Python 3.12.
