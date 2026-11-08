# Task 1 — Custom MCP Server with Strict Validation & Transport Handling

A runnable [Model Context Protocol](https://modelcontextprotocol.io) server in Python, built on
the official `mcp` SDK (v2.3). It exposes two customer-support tools over the **stdio**
transport, validates every argument with **Pydantic** in strict mode, and reports problems with
**standard JSON-RPC 2.0 error codes**.

## Reading guide

Three files answer the task. Read them first:

1. [`src/customer_mcp/schemas.py`](src/customer_mcp/schemas.py) — the Pydantic contracts
   (**Validation**).
2. [`src/customer_mcp/server.py`](src/customer_mcp/server.py) — `tools/list`, `tools/call`, and
   the JSON-RPC error mapping (**Protocol compliance**).
3. [`src/customer_mcp/__main__.py`](src/customer_mcp/__main__.py) — the stdio transport, stderr
   logging, and the stdout guard (**STDIO isolation**).

The rest is supporting code: `tools.py` (tool definitions), `errors.py` (error construction),
`repository.py` (sample data behind a swappable interface), `transport.py` (error replies that
the SDK does not send), and `logging_config.py`.

## The task

> Write a runnable MCP server (TypeScript or Python) using the official SDK that exposes two
> tools:
> 1. `get_customer_record` (input: `customer_id` string formatted as `CUST-XXXXX`).
> 2. `trigger_refund` (inputs: `customer_id`, `amount` positive float, `reason` string with
>    minimum length of 10).
>
> **Requirements**
> - Enforce strict input schema validation (Zod or Pydantic). Reject invalid formats with
>   standard MCP JSON-RPC error codes.
> - Connect via stdio transport. Ensure stdout is strictly reserved for JSON-RPC messages and
>   system/debug logs write exclusively to stderr.
>
> **Evaluation criteria**
> - **STDIO isolation** — stdout is pure JSON-RPC, with no accidental `print` statements.
> - **Protocol compliance** — standard JSON-RPC error mapping and execution flow.
> - **Validation** — robust schema definitions and edge-case handling for malformed input.

## What is implemented

### Tools

| Tool | Input | Success result | Hints |
|---|---|---|---|
| `get_customer_record` | `customer_id` | `CustomerRecord` (`customer_id`, `name`, `email`, `tier`, `account_status`) | read-only |
| `trigger_refund` | `customer_id`, `amount`, `reason` | `RefundReceipt` (`refund_id`, `customer_id`, `amount`, `reason`, `status`) | not read-only, not idempotent |

Each tool advertises both an `inputSchema` and an `outputSchema` in `tools/list`. Both are
generated from the same Pydantic models that validate the call, so the advertised contract and
the enforced contract cannot drift apart. Successful calls return `structuredContent` plus the
same JSON as a text block, for clients that only read text.

### Validation rules

All input models use `strict=True` (no type coercion), `extra="forbid"` (unknown fields are
rejected), and `frozen=True` (validated input is immutable).

| Field | Rule | Rejected examples |
|---|---|---|
| `customer_id` | `^CUST-[0-9]{5}$`, case-sensitive, string only | `CUST-1234`, `CUST-123456`, `cust-12345`, `CUST12345`, `" CUST-12345"`, `"CUST-12345\n"`, `CUST-١٢٣٤٥` (Arabic-Indic digits), `CUST-１２３４５` (full-width digits), `12345` |
| `amount` | number (int or float), `> 0`, `<= 1,000,000`, finite, at most 2 decimal places | `0`, `-0.0`, `-1`, `NaN`, `Infinity`, `10.005`, `0.001`, `1000000.01`, `1e20`, `"10"`, `true`, `null` |
| `reason` | string; surrounding whitespace trimmed, then 10–500 characters; no control or invisible formatting characters (tab and newline are allowed) | `"short"`, `"          "` (10 spaces), 12 non-breaking spaces, 10 zero-width spaces, a bidi override (`U+202E`), a NUL byte, an ANSI escape, 501 characters, `1234567890` |

Notes on the choices:
- The pattern uses `[0-9]`, not `\d`. Pydantic's Rust regex engine treats `\d` as *any* Unicode
  digit, so `\d` would accept `CUST-١٢٣٤٥`. A test pins this.
- `amount` is a money value, so more than two decimal places is rejected, not rounded. The
  ceiling (`MAX_REFUND_AMOUNT` in `schemas.py`) stops absurd values such as `1e20`.
- `reason` is trimmed before the length check, so ten spaces cannot pass the 10-character rule.
  Unicode categories `Cc` (control) and `Cf` (format) are rejected, so invisible text such as
  zero-width spaces cannot pass the length rule either, and bidi overrides or terminal escape
  sequences cannot reach downstream systems.

### Error mapping

The server follows the MCP specification's two error channels:

| Situation | Channel | Code / shape |
|---|---|---|
| Invalid JSON on stdin (including a blank line) | JSON-RPC error | `-32700 Parse error`, `id: null` |
| Valid JSON that is not a JSON-RPC request (no `method`, `"jsonrpc": "1.0"`, batch array, scalar) | JSON-RPC error | `-32600 Invalid Request`, `id` echoed when it can be recovered |
| A request whose `id` is missing or not a string/integer (`null`, `32.5`, `true`, `{}`) | JSON-RPC error | `-32600 Invalid Request`, `id: null` |
| Unknown method (e.g. `resources/list`) | JSON-RPC error | `-32601 Method not found` |
| `tools/call` with a missing or non-string `name`, or non-object `arguments` | JSON-RPC error | `-32602 Invalid request parameters` (raised by the SDK before the tool code runs) |
| Unknown tool name | JSON-RPC error | `-32602`, message `Unknown tool: <name>` |
| Arguments fail validation | JSON-RPC error | `-32602`, `data: {tool, errors: [{field, message, type}]}` |
| Well-formed `customer_id` that does not exist | Tool result | `isError: true`, text `Customer CUST-99999 not found` |
| Unexpected exception (database down, bug) | JSON-RPC error | `-32603 Internal server error`, no details |

Why an unknown customer is an `isError` result and not a JSON-RPC error: the MCP spec reserves
JSON-RPC errors for protocol problems (unknown tool, invalid arguments, server faults) and
uses `isError: true` for failures during tool execution. The request is valid, but the lookup
finds nothing. The model can read the message and recover, for example by asking the user for
the correct ID.

### What the server never repeats

A hostile client must not be able to reflect a payload back through an error or forge a line in
the server log. So:
- `-32602` error data names only the fields that the tool **declares** (`customer_id`,
  `amount`, `reason`). An unknown argument key is shown as `<unexpected field>`, never by its
  own name. Rejected values are never included.
- `-32603` never includes the exception text. The full stack trace goes to stderr.
- This server's log lines record tool names, declared field names, and outcome labels (for
  example `customer_not_found`). They never record argument values, caller-chosen key names, or
  customer IDs. This holds at the default `INFO` level. At `DEBUG`, the SDK's own debug lines
  can include raw protocol messages, so do not run production at `DEBUG`.

Two deliberate exceptions, both sent only back to the caller who sent the value: the `isError`
text for an unknown customer contains the ID, which helps the model recover, and the
unknown-tool message contains the tool name, in the format of the MCP specification's own
example (`Unknown tool: <name>`).

### STDIO isolation (two layers)

1. **File-descriptor level (SDK).** While serving, `stdio_server()` duplicates fd 1 for the
   protocol and points fd 1 at stderr. Writes from C extensions or child processes miss the wire.
2. **Python level (this project).** `__main__.py` wraps the serve loop in
   `redirect_stdout(sys.stderr)`. Layer 1 alone is not enough: during development, a test
   showed that a stray `print()` in a handler stays in Python's `sys.stdout` buffer, and Python
   flushes it onto the wire at exit, *after* the SDK has pointed fd 1 back at the protocol pipe.
   The redirect must start *after* the SDK claims fd 1, because the claim checks that
   `sys.stdout` is still fd 1.

Logging is configured once, to `sys.stderr` only (`logging_config.py`). Ruff rule `T20` and a
test both forbid `print()` in the source.

### JSON-RPC compliance gaps closed in the SDK

The SDK leaves two kinds of bad input with no reply at all. A client that sends one waits until
it times out. `transport.ProtocolErrorReplyingStream` wraps the read stream and closes both:

| Gap in SDK 2.3 | Fix |
|---|---|
| An undecodable line becomes an exception on the read stream, and the runner drops it | Reply `-32700` (invalid JSON) or `-32600` (not a request object) |
| A request with an invalid `id` parses as a *notification* (the SDK models ignore unknown fields), so the runner never replies | In MCP every notification method starts with `notifications/`, so any other "notification" is a request without a valid id: reply `-32600` |

### Architecture

```
src/customer_mcp/
├── __main__.py        Entry point: stderr logging, stdio transport, stdout redirect
├── server.py          build_server(repository): tools/list and tools/call
├── tools.py           Tool registry: each ToolSpec holds its models, MCP hints, and handler
├── schemas.py         Pydantic input/output contracts (single source of truth)
├── errors.py          Error taxonomy and JSON-RPC error construction
├── repository.py      Domain entities, CustomerRepository protocol, in-memory adapter
├── transport.py       Replies to malformed JSON-RPC instead of dropping it
└── logging_config.py  stderr-only logging, level from CUSTOMER_MCP_LOG_LEVEL
```

Design decisions:
- **Low-level `Server` instead of the high-level `MCPServer` decorator API.** `MCPServer`
  converts argument-validation failures into `isError` results. The task requires JSON-RPC error
  codes for invalid input, so the server owns validation and raises `-32602` itself.
- **Repository port.** `build_server()` takes any object that satisfies the
  `CustomerRepository` protocol. The in-memory adapter can be swapped for a database or CRM
  client without touching the protocol layer. Tests use the same seam to inject failing
  repositories.
- **Registry of `ToolSpec`s.** Adding a tool means adding one entry with its models and handler.
  The protocol layer does not change. Output models are built straight from the domain
  entities (`from_attributes=True`), so there is no field-by-field copying.
- **Public SDK surface only.** The transport wrapper defines its own small stream protocols
  instead of importing the SDK's private `_stream_protocols` module, so an SDK upgrade cannot
  break the import.

Sample customers: `CUST-12345` (Ada Lovelace), `CUST-00001` (Alan Turing), `CUST-54321`
(Grace Hopper). Any other well-formed ID is "not found".

## Tests

120 tests, about 98% line coverage. The coverage run also measures the server subprocess, and
it fails below 90%. Each failure message names the field, the error code, or the exact stray
stdout line, so a failure points at the fix.

Tested on Windows 11 with Python 3.11.

The tests sit at three seams, from fastest to most realistic:

| File | Seam | Marker | What it proves | Criterion |
|---|---|---|---|---|
| `tests/test_schemas.py` | Pydantic models | `unit` | Every validation rule and edge case, one field at a time | Validation |
| `tests/test_server.py` | Official SDK `Client`, connected in-process to `build_server()` | `integration` | `tools/list` schemas and hints, results, `-32602` and `-32603` with their `data`, no leaks | Protocol compliance, Validation |
| `tests/test_stdio.py` | Real `python -m customer_mcp` subprocess, raw newline-delimited JSON-RPC | `e2e` | Stdout purity, logs on stderr, wire-level errors, clean shutdown | STDIO isolation, Protocol compliance |

What is in each file:

**`test_schemas.py`**
- `customer_id`: valid IDs; 12 malformed strings (length, case, missing dash, whitespace,
  trailing newline, Unicode digits, unanchored prefix); non-string types; missing field;
  unknown extra field.
- `amount`: valid values from `0.01` to the `1,000,000` ceiling; zero, negative zero,
  negatives, `NaN`, `±Infinity`, three decimals, sub-cent, above the ceiling, `1e20`,
  numeric string, boolean, `null`.
- `reason`: exactly 10 and exactly 500 characters; multi-line text; whitespace trimming;
  9 characters, whitespace-only, non-breaking spaces only, zero-width spaces only, bidi
  override, NUL byte, ANSI escape, short after trim, 501 characters, empty, integer, `null`.
- Shape: all invalid fields are reported together; extra fields rejected; validated input is
  immutable.

**`test_server.py`**
- `tools/list` returns exactly the two tools, with `pattern`, `exclusiveMinimum`, `maximum`,
  `minLength`, `maxLength`, `required`, and `additionalProperties: false` in the input schema,
  and the correct read-only and idempotent hints.
- Successful calls return `structuredContent`; each refund gets a unique `refund_id`.
- An unknown customer gives `isError: true` for both tools.
- Invalid arguments and boundary violations give `-32602` with the exact failing fields in
  `data`. A missing `arguments` object also gives `-32602`.
- The error `data` does not echo a rejected value or an unexpected key name (XSS-style
  payloads are used).
- An unknown tool gives `-32602`, with the tool name in the message.
- A failing database read (lookup) and a failing database write (refund) both give `-32603`,
  and the password inside the exception text appears nowhere in the error.

**`test_stdio.py`**
- A full session (initialize, list, success, invalid, not found) writes only JSON-RPC 2.0 lines
  to stdout and exits with code 0.
- Logs reach stderr. Argument values, unknown customer IDs, and a caller-chosen key that
  contains a newline and a fake log line never appear in the logs or the error.
- A handler that calls `print()` does not corrupt stdout; the text appears on stderr.
- A static AST scan finds no `print()` and no `sys.stdout` outside the entry point.
- Wire protocol: `initialize` handshake, structured results, `-32602` with field data,
  `isError` for an unknown customer, `-32601` for an unknown method, `-32602` for an unknown
  tool.
- Malformed `tools/call` params (string `arguments`, list `arguments`, missing `name`,
  non-string `name`, missing `params`) give `-32602`.
- Malformed input: invalid JSON and a blank line give `-32700`, and the server keeps serving;
  five kinds of invalid request objects give `-32600` with the `id` echoed when present; five
  kinds of invalid or missing `id` give `-32600` with `id: null`.
- A genuine notification (`notifications/cancelled`) gets no reply.
- The official SDK `Client` works over stdio, through its default `server/discover`
  negotiation.
- The server exits with code 0 when stdin closes.

## How to run

Prerequisites: Python 3.11+ and [uv](https://docs.astral.sh/uv/). The optional MCP Inspector
also needs Node.js.

All commands below run from the `task1-mcp-server` folder:

```bash
cd task1-mcp-server
```

```bash
uv sync
```

### Run the server

```bash
uv run customer-mcp
```

The server speaks JSON-RPC on stdin and stdout, so it is normally started by an MCP host. To
try it by hand, use the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run customer-mcp
```

To register it with an MCP host such as Claude Desktop:

```json
{
  "mcpServers": {
    "customer-mcp": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/task1-mcp-server", "run", "customer-mcp"]
    }
  }
}
```

Set `CUSTOMER_MCP_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR`) to change the log level.
The default is `INFO`.

### Run the tests

All tests:

```bash
uv run pytest
```

One seam at a time:

```bash
uv run pytest -m unit
```

```bash
uv run pytest -m integration
```

```bash
uv run pytest -m e2e
```

With coverage (includes the server subprocess; fails below 90%):

```bash
uv run pytest --cov
```

### Quality checks

Lint and format check:

```bash
uv run ruff check src tests
```

```bash
uv run ruff format --check src tests
```

Security scan:

```bash
uv run bandit -c pyproject.toml -r src
```

## Assumptions

- `CUST-XXXXX` means the literal prefix `CUST-` followed by exactly five ASCII digits.
- "Positive float" accepts JSON integers too (`10` is a valid amount); JSON has one number type.
- `amount` stays a float, as the task specifies. The two-decimal rule rejects values that a
  float cannot represent as whole cents, and a real refund backend would convert the
  validated value to an integer number of cents or a `Decimal` at its own boundary.
- Refunds are not stored and are always `accepted`. Balance checks, payment-provider calls,
  idempotency keys, and rate limiting belong to a real refund backend or to the gateway in
  front of this server (Tasks 2 and 4), and are out of scope here. A stdio server has exactly
  one client: the host process that started it.
