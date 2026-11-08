# Task 2 — MCP Security Gateway Proxy (Tool Filtering & Auth)

A lightweight HTTP JSON-RPC reverse proxy in Python. It sits between an AI agent (the MCP
client) and a downstream MCP server. It reads the caller's role from a signed Bearer token,
forwards `tools/list` unchanged, and blocks a `tools/call` of an `admin_` tool by a non-admin
with `-32001: Unauthorized Tool Call`. The downstream server never gets a blocked call.

Stack: Starlette, `httpx2` (the HTTP client of the official `mcp` SDK), PyJWT, and uvicorn. The
downstream mock is a real MCP server on the official `mcp` SDK (v2.3).

## Reading guide

1. [`jsonrpc.py`](src/mcp_gateway/jsonrpc.py) — strict JSON-RPC parsing and the error codes
   (**Parsing JSON-RPC wire format**).
2. [`app.py`](src/mcp_gateway/app.py) and [`proxy.py`](src/mcp_gateway/proxy.py) — the request
   pipeline and the HTTP forwarding (**Proxy middleware and request/response forwarding**).
3. [`policy.py`](src/mcp_gateway/policy.py) and [`auth.py`](src/mcp_gateway/auth.py) — the role
   check and the `admin_` rule (**Method-level authorization and clean error handling**).

Supporting code: `config.py` (settings), `__main__.py` (starts the gateway), `token_cli.py`
(development tokens), and `mock_server.py` (the downstream MCP server).

## The task

> Build a lightweight HTTP/JSON-RPC reverse proxy (in Node.js or Python) that acts as an
> **MCP Gateway** sitting between an AI agent client and a downstream mock MCP server.
>
> - Read an incoming `Bearer <token>` HTTP header and extract the user's role (e.g., admin or
>   viewer).
> - If method is `tools/list`, forward the request transparently to the downstream server.
> - If method is `tools/call`, inspect `params.name`. If it starts with `admin_` (e.g.,
>   `admin_reset_key`), verify that the token role is admin. If not authorized, intercept and
>   return a JSON-RPC Error (`-32001: Unauthorized Tool Call`) directly without calling the
>   downstream server.
>
> **Evaluation criteria:** parsing JSON-RPC wire format structures correctly; proxy middleware
> construction and HTTP request/response forwarding; fine-grained, method-level authorization
> logic and clean error handling.

## How it works

Every `POST /mcp` goes through these steps. Every refusal happens **before** the downstream
call.

```
AI agent ──POST /mcp──▶ 1. authenticate   Bearer JWT → role             (401 on failure)
                        2. read body      up to the size limit           (413 on failure)
                        3. parse          one strict JSON-RPC message    (400 on failure)
                        4. authorize      admin_ tools need role=admin   (-32001 / -32602)
                        5. check headers  one Mcp-* value each, = body   (400 on failure)
                        6. forward ──────────────────────────────────▶ downstream MCP server
AI agent ◀─────────── streamed reply (JSON or SSE), status and headers unchanged ◀──┘
```

**Authentication.** The token is an HS256 JWT signed with `GATEWAY_JWT_SECRET`. The gateway
needs a valid signature, HS256 only (so `alg: none` fails), an `exp` in the future, and a
`role` of `admin` or `viewer`. An unknown role fails closed. The gateway checks the token
before it reads the body.

**Authorization.** Only `tools/call` is gated: `params.name` must be a string, and a name that
starts with `admin_` needs the `admin` role. The prefix check ignores case, so
`ADMIN_reset_key` is also blocked. `get_admin_contact` is not an admin tool. A `tools/call`
without an `id` (a notification) is checked too. Every other method (`tools/list`,
`initialize`, notifications) and every client response (an answer to a server request, such as
sampling) is forwarded unchanged.

**Parsing.** The gateway decides from the body, so it refuses a body that another parser could
read in a different way: duplicate keys, `NaN`, bad UTF-8, and batches (MCP 2025-06-18 removed
batching). Ids must be a string or an integer.

**Forwarding.**
- The body is forwarded byte for byte, so the downstream server reads exactly what was checked.
- Request headers pass an allow-list: `Content-Type`, `Accept`, `Last-Event-ID`, and `Mcp-*`.
  The token and cookies are never forwarded.
- MCP 2026-07-28 copies the method and the tool name into `Mcp-Method` and `Mcp-Name`. These
  must match the body, and a repeated `Mcp-*` header is refused. Otherwise one copy could pass
  the check while the downstream server routes by the other.
- The reply streams back with its status and headers; SSE streams through as it arrives.
  Hop-by-hop headers are dropped.
- `GET /mcp` (the server's event stream) and `DELETE /mcp` (end of session) carry no JSON-RPC
  message. They need a valid token and pass the same header allow-list. The `GET` stream has no
  read timeout, because it can stay quiet for a long time.

**Audit log.** Each decision goes to stderr (`allowed`, `denied`, `rejected`). The token is
never logged. Caller values are logged with `repr()`, so a newline cannot forge a log line.

### Error mapping

| Situation | HTTP | Code | Message |
|---|---|---|---|
| Missing, bad, or expired token | 401 | `-32000` | `Authentication required` |
| Not JSON, bad UTF-8, duplicate keys, `NaN` | 400 | `-32700` | `Parse error` |
| Batch, non-object, bad `jsonrpc` / `method` / `id` / `params`, bad response shape | 400 | `-32600` | `Invalid Request` |
| `Mcp-*` header disagrees with the body, or is repeated | 400 | `-32600` | `Invalid Request: ...` |
| Body over the size limit | 413 | `-32600` | `Request body too large` |
| `tools/call` without a string `params.name` | 200 | `-32602` | `Invalid params: params.name must be a string` |
| **Non-admin calls an `admin_` tool** | **200** | **`-32001`** | **`Unauthorized Tool Call`** |
| Downstream server unavailable / too slow | 502 / 504 | `-32603` | `Downstream MCP server unavailable` / `timed out` |

An error reply keeps the request `id` when the gateway could read a valid one. A refused
notification gets an HTTP error status instead of 200 (403 for `-32001`, 400 for `-32602`), as
MCP over HTTP requires. No error reply repeats the token, a stack trace, or the downstream
address.

## Tests

93 tests in two seams (a seam is the public boundary that a test calls). Coverage is 99%,
including the gateway subprocess. Tested on Windows 11 with Python 3.12.

**Gateway HTTP seam** ([`tests/test_gateway.py`](tests/test_gateway.py), 78 tests). The real
Starlette app runs in-process. A recording downstream keeps every request it receives, so each
refusal test also proves that **the downstream server got no call**. The tests cover:
- the `-32001` block, including upper case, a JSON `\u0061` escape, and a notification;
- the permitted calls;
- 11 kinds of bad token;
- every parse, invalid-request, and invalid-params case in the table above;
- header forwarding, including the repeated-`Mcp-Name` bypass;
- reply streaming (JSON, SSE, `202`, a downstream `404`);
- 502 and 504;
- the size limit, `GET`/`DELETE`, and the audit log.

**Real processes** ([`tests/test_e2e.py`](tests/test_e2e.py), 15 tests). The real gateway and
the real mock MCP server run as separate processes. The **official MCP SDK client** connects
over Streamable HTTP, with tokens from the `issue-token` command. It proves that:
- `tools/list` works through the gateway;
- a viewer can call a normal tool;
- a viewer gets `-32001` on `admin_reset_key`, and an admin can run it;
- a session-based client works, with `GET` and `DELETE` through the gateway;
- the gateway refuses to start with any of 10 bad settings, with a one-line reason and no
  traceback.

## How to run

Prerequisites: Python 3.11+ and [uv](https://docs.astral.sh/uv/). Run every command from the
`task2-mcp-gateway` folder in a bash shell.

```bash
cd task2-mcp-gateway
```

```bash
uv sync
```

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_JWT_SECRET` | required | HS256 secret, at least 32 bytes |
| `GATEWAY_DOWNSTREAM_URL` | `http://127.0.0.1:8001/mcp` | Downstream MCP server |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `8000` | Where the gateway listens |
| `GATEWAY_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `GATEWAY_MAX_BODY_BYTES` | `1048576` | Largest request body |
| `GATEWAY_DOWNSTREAM_TIMEOUT_SECONDS` | `30` | Wait for the downstream server |
| `MOCK_MCP_HOST` / `MOCK_MCP_PORT` | `127.0.0.1` / `8001` | Where the mock server listens |

The gateway checks every setting at startup and stops with a one-line reason on a bad value.

### Start the servers

1. In each terminal, set the same secret:

   ```bash
   export GATEWAY_JWT_SECRET="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(48))')"
   ```

   Generate it once, then copy the same value to the other terminals.

2. In terminal 1, start the mock MCP server:

   ```bash
   uv run mock-mcp-server
   ```

3. In terminal 2, start the gateway:

   ```bash
   uv run mcp-gateway
   ```

4. In terminal 3, make a viewer token:

   ```bash
   VIEWER=$(uv run issue-token --role viewer)
   ```

### Try it

A viewer calls an admin tool, and the gateway blocks it:

```bash
curl -s http://127.0.0.1:8000/mcp -H "Authorization: Bearer $VIEWER" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{"key_id":"k-1"}}}'
```

```json
{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"Unauthorized Tool Call"}}
```

A request without a token gets HTTP 401 with `-32000`. The allowed path needs the MCP handshake
first, so use an MCP client for it: the MCP Inspector (Streamable HTTP, URL
`http://127.0.0.1:8000/mcp`, header `Authorization: Bearer <token>`), or the e2e tests.

### Tests and checks

```bash
uv run pytest --cov
```

```bash
uv run pytest -m integration
```

```bash
uv run pytest -m e2e
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

- **Signed JWT, not a token table.** The role travels in the token, as with a real identity
  provider. `issue-token` exists only for local runs.
- **"Starts with `admin_`" ignores case**, so the gateway fails closed.
- **Only `tools/call` is gated.** Every other method must pass for the MCP session to work.
  `tools/list` still shows `admin_` tools to a viewer; hiding them from the list is a different
  feature that the task does not ask for.
- **The mock server has no authorization of its own**, so the e2e tests prove that the gateway
  alone blocks `admin_reset_key`.
- **MCP tool names are ASCII**, so `Mcp-Name` is compared to `params.name` as plain text.
