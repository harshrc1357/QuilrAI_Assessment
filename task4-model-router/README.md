# Task 4 — Rate-Limiting & Model Fallback Router for LLM Gateways

A model-routing gateway in Python for chat completion requests. Each tenant API key gets a
budget of 50,000 tokens per minute in a sliding window, stored in an on-disk SQLite database.
Each request goes to the primary model provider; on a `429`, a timeout after 3000 ms, or
another provider outage (`5xx`, `408`, connection failure), it goes to the secondary provider. Every error is one standard
payload, with no upstream text, stack trace, or internal detail.

Stack: Starlette, `httpx`, anyio, uvicorn, and Python's built-in `sqlite3`.

## Reading guide

1. [`limiter.py`](src/model_router/limiter.py) — the SQLite sliding-window limiter
   (**rate-limiter state eviction and token tracking**).
2. [`router.py`](src/model_router/router.py) — the provider call with its deadline, and the
   failover (**async concurrency, timeout race conditions, graceful fallback**).
3. [`app.py`](src/model_router/app.py) and [`errors.py`](src/model_router/errors.py) — the
   endpoint: tenant key, reserve, route, settle; and the error payload
   (**standardized error sanitization**).

Supporting code: `config.py` (settings), `__main__.py` (starts the gateway), and
`mock_provider.py` (a stand-in LLM provider).

## The task

> Write a resilient model-routing module for an LLM Gateway that accepts incoming completion
> requests.
>
> - Implement a token-aware sliding window rate limiter (e.g., maximum 50,000 tokens/minute
>   per tenant API key).
> - If the primary model endpoint returns a 429 Too Many Requests status or times out after
>   3000ms, automatically failover to a secondary backup model provider.
> - Ensure error responses return a standardized gateway error payload without leaking raw
>   upstream stack traces or internal implementation details.
> - Use on disk sqlite for database
>
> **Evaluation criteria:** async concurrency handling and timeout race conditions; accurate
> rate-limiter state eviction and token tracking logic; graceful fallback mechanics and
> standardized error sanitization.

## How it works

```
client ──POST /v1/chat/completions──▶ 1. tenant key   Bearer key in ROUTER_TENANT_KEYS  (401)
          Authorization: Bearer <key>  2. check body   messages, max_tokens               (400)
                                       3. reserve      estimated tokens in SQLite         (429)
                                       4. route        primary ──fail──▶ secondary   (502/504)
                                       5. settle       real usage replaces the estimate
client ◀── provider reply + x-gateway-provider, x-ratelimit-remaining-tokens ◀──┘
```

**Token tracking.** The provider reports the real usage only after the call, but the limit
must be checked before it. So the gateway **reserves** an estimate first: the prompt size
(its JSON length / 4) plus `max_tokens` (256 if absent). The provider always gets that
`max_tokens`, so it cannot write more than the reservation allows. After the call, the
gateway **settles** the row to the provider's `usage.total_tokens`. A failed request is
**released** (its tokens come back). Both steps are shielded from cancellation: a client
that leaves before the reply gets its tokens back, and one that leaves after it still pays.

**The sliding window** is the last 60 seconds of rows for the tenant, at any moment (not
fixed minutes). Each reservation:

1. starts a write transaction (`BEGIN IMMEDIATE`), so no other request, in this process or
   another one on the same file, can check the budget at the same time;
2. **evicts** the rows of every tenant that are 60 s old or older (`DELETE`), so the table
   stays small, also when a tenant stops sending requests;
3. sums the remaining rows; if the request does not fit, refuses it with `429` and a
   `Retry-After` that is the time until the oldest rows that must leave have left;
4. otherwise inserts the reservation and commits.

A request larger than the whole limit can never fit, so it gets `400 request_too_large`, not
a `429` that the client would retry forever. The database stores a SHA-256 hash of the
tenant key, never the key. WAL mode lets reads and writes go on at the same time.

**Failover and the timeout race.** The primary call has one deadline for the whole exchange
(`anyio.fail_after`, 3000 ms by default), body included. httpx's own timeouts apply to each
network read, so a provider that trickles its reply slowly would pass them. When the deadline
passes, the call is cancelled, so a reply that arrives late is never used and never mixed
with the secondary's reply.

| Primary result | What the gateway does |
|---|---|
| `200` with a JSON object | Returns it (`x-gateway-provider: primary`) |
| `408`, `429`, `5xx`, timeout, connection failure, unreadable reply | Calls the secondary |
| Other `4xx` | `400 invalid_request`: the request itself is bad, so the secondary would reject it too |

Each provider gets its own model name and API key. The tenant's key is never sent upstream.

### Error mapping

Every error has this shape, and its `request_id` is also in the `x-request-id` header:

```json
{"error": {"code": "rate_limit_exceeded", "message": "The tenant's token limit per minute is used up", "request_id": "3f2a..."}}
```

| Situation | HTTP | `code` |
|---|---|---|
| Missing or unknown tenant key | 401 | `unauthorized` |
| Bad body, `stream: true`, or the provider rejects the request | 400 | `invalid_request` |
| Request needs more tokens than the limit | 400 | `request_too_large` |
| Tenant's window is full | 429 | `rate_limit_exceeded` (with `Retry-After`) |
| Both providers fail | 502 | `upstream_unavailable` |
| The secondary times out too | 504 | `upstream_timeout` |
| Unknown path / wrong method | 404 / 405 | `not_found` / `method_not_allowed` |
| Any other failure in the gateway | 500 | `internal_error` |

The message is fixed text per code. Upstream bodies, provider URLs, provider keys, and
exception text never reach the client; the details go to the server log with the
`request_id`.

## Tests

79 tests in three seams (a seam is the public boundary that a test calls). Coverage is 100%,
including the gateway subprocess. Tested on Windows 11 with Python 3.12.

**Limiter** ([`tests/test_limiter.py`](tests/test_limiter.py), 17 tests), with a fake clock
and a real SQLite file: the budget fills and refuses; settle and release; separate tenants;
rows leave at exactly 60 s; the window slides one row at a time; exact `Retry-After`
values; expired rows deleted, also an idle tenant's; **120 concurrent reservations accept
exactly 50**; usage survives a new limiter on the same file; the raw key is not in the file.

**Gateway HTTP** ([`tests/test_gateway.py`](tests/test_gateway.py), 48 tests). The real app
runs in-process with two recording mock providers. It covers failover for each primary
failure (including a reply body that trickles past the deadline, cut off in time), no
failover on a `4xx`, each row of the error table, the usage settle and the refund, the
default `max_tokens` sent upstream, a client that leaves during the settle, and that no
error holds a provider URL, key, or upstream text.

**Real processes** ([`tests/test_e2e.py`](tests/test_e2e.py), 14 tests). The real gateway
and two mock provider processes:
- the primary answers; a primary `429` fails over to the secondary;
- **a primary slower than 3000 ms fails over at the real 3000 ms deadline**;
- both failing gives a clean `502`;
- **the token window survives a gateway restart** (the SQLite file on disk);
- the gateway refuses to start with any of 9 bad settings, with a one-line reason.

## How to run

Prerequisites: Python 3.11+ and [uv](https://docs.astral.sh/uv/). Run every command from the
`task4-model-router` folder in a bash shell.

```bash
cd task4-model-router
```

```bash
uv sync
```

| Variable | Default | Meaning |
|---|---|---|
| `ROUTER_TENANT_KEYS` | required | Comma-separated tenant API keys, each at least 16 characters |
| `ROUTER_TOKENS_PER_MINUTE` | `50000` | Each tenant's budget in the 60 s window |
| `ROUTER_DB_PATH` | `router.db` | The SQLite file |
| `ROUTER_PRIMARY_URL` / `_MODEL` / `_API_KEY` | `http://127.0.0.1:8101/v1/chat/completions` / `primary-model` / none | The primary provider |
| `ROUTER_PRIMARY_TIMEOUT_MS` | `3000` | The primary's deadline |
| `ROUTER_SECONDARY_URL` / `_MODEL` / `_API_KEY` | `http://127.0.0.1:8102/v1/chat/completions` / `secondary-model` / none | The secondary provider |
| `ROUTER_SECONDARY_TIMEOUT_MS` | `30000` | The secondary's deadline |
| `ROUTER_HOST` / `ROUTER_PORT` | `127.0.0.1` / `8000` | Where the gateway listens |

### Start the servers

1. In terminal 1, start the mock primary provider:

   ```bash
   MOCK_NAME=primary MOCK_PORT=8101 uv run mock-llm-provider
   ```

2. In terminal 2, start the mock secondary provider:

   ```bash
   MOCK_NAME=secondary MOCK_PORT=8102 uv run mock-llm-provider
   ```

3. In terminal 3, start the gateway:

   ```bash
   ROUTER_TENANT_KEYS=demo-tenant-key-0001 uv run model-router
   ```

### Try it

The mock provider obeys an instruction in the message: `[primary:429]`, `[primary:500]`, or
`[primary:slow]` (the same with `secondary`). This request makes the primary answer `429`:

```bash
curl -si http://127.0.0.1:8000/v1/chat/completions -H "Authorization: Bearer demo-tenant-key-0001" -H "Content-Type: application/json" -d '{"messages":[{"role":"user","content":"Hi [primary:429]"}],"max_tokens":100}'
```

The reply has `x-gateway-provider: secondary` and the text
`Hello from the secondary model (secondary-model)`. Use `[primary:slow]` to see the failover
after 3 seconds, and `"max_tokens": 49000` twice to see the `429`.

### Tests and checks

```bash
uv run pytest --cov
```

```bash
uv run ruff check src tests
```

```bash
uv run ruff format --check src tests
```

```bash
uv run bandit -c pyproject.toml -r src
```

## Assumptions and decisions

- **OpenAI-style chat completions, not streamed.** The task is about routing and limits;
  `stream: true` gets `400`.
- **Failover also on `5xx`, `408` and connection failures**, not only on `429` and the
  timeout: they are the same kind of provider outage. Any other `4xx` is not retried.
- **The prompt estimate is rough on purpose** (4 characters per token). The completion part
  is exact, because the provider gets `max_tokens`. The real usage replaces the estimate
  after each call, so the window holds real numbers. If a prompt has more tokens than the
  estimate, requests that run at the same time can take a tenant a little over the limit.
  An exact count needs each model's tokenizer, which the task does not ask for.
- **A token counts at its reservation time**, so a long call's tokens leave the window 60 s
  after the call started.
- **Tenant keys come from settings.** A real deployment would read them from a key store.
