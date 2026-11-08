# Task 3 — LLM Gateway Streaming Guardrail (PII Redaction)

An LLM gateway endpoint in Python. It forwards an OpenAI-style streaming chat completion
request to an LLM provider, and streams the reply back. While the reply streams, it replaces
email addresses, US SSNs, and credit card numbers with `[REDACTED]`, also when one is split
across many chunks. It holds back only the few characters that could still become PII, so
the first token is not delayed and memory stays bounded.

Stack: Starlette, `httpx`, uvicorn, and the [`regex`](https://pypi.org/project/regex/)
package (for partial matching).

## Reading guide

1. [`redactor.py`](src/pii_guardrail/redactor.py) — the incremental redactor and its patterns
   (**regex pattern matching over partial text streams**, **buffer state management**).
2. [`sse.py`](src/pii_guardrail/sse.py) — reads the provider's SSE events and redacts the
   generated text in each one (**asynchronous stream chunking**).
3. [`app.py`](src/pii_guardrail/app.py) — the endpoint: request check, forwarding, provider
   errors, streaming reply (**low-latency proxying design**).

Supporting code: `config.py` (settings), `__main__.py` (starts the gateway), and
`mock_provider.py` (a stand-in LLM provider that streams the user message back).

## The task

> Implement an **LLM Gateway** proxy endpoint that routes text generation requests to an LLM
> provider and streams the response back to the client.
>
> - Intercept the returning response chunk stream in real time.
> - Parse incoming stream deltas to detect and redact sensitive patterns (such as Emails,
>   SSNs, or credit card numbers), replacing them with [REDACTED].
> - Ensure the stream stays responsive without accumulating the full response in memory,
>   minimizing Time To First Token (TTFT) and overall stream latency.
>
> **Evaluation criteria:** efficient asynchronous stream chunking and buffer state
> management; performant string/regex pattern matching over partial text streams; memory
> efficiency and low-latency proxying design.

## How it works

```
client ──POST /v1/chat/completions (stream: true)──▶ gateway ──▶ LLM provider
client ◀── SSE: data: {..."delta":{"content":"Mail [REDACTED]"}...} ◀── redact ◀── SSE chunks
```

**The problem.** PII can be split across chunks: `"Mail jane.d"`, `"oe@exam"`, `"ple.com"`.
A redactor that looks at one chunk at a time misses it. A redactor that waits for the whole
reply has a bad TTFT and holds the whole reply in memory.

**The redactor** (`StreamRedactor`) keeps one piece of state: the *held text*, the tail that
could still grow into a match. For each chunk it:

1. Joins the held text and the new chunk.
2. Finds the **hold point** once: the leftmost position where the rest of the text is the
   start of some PII match. It uses `regex` partial matching with a copy of the pattern that
   can never complete, so the search returns only "could still match" positions.
3. Replaces each complete match before the hold point with `[REDACTED]`, and sends all text
   before the hold point at once.
4. Keeps the text from the hold point as the new held text.

So `"Write to jane.d"` sends `"Write to "` at once and holds `"jane.d"`. A plain word is held
only until the next space or punctuation mark. A complete match that more input could extend
(16 card digits and a space) is held too, so a 19-digit card is never cut in two.

**Bounded memory.** Every quantifier in the patterns has a maximum, so a match is at most 601
characters (a full-length email address). The held text can never be longer, whatever the
reply length. One SSE line is limited to 1,048,576 characters, so a provider line without an
end cannot fill memory either.

**Linear time.** The hold point is found once per chunk, not once per match. A 24 KB chunk
with 2,000 SSNs takes about 0.04 s.

| Kind | Matches |
|---|---|
| Email | `local@domain.tld`: local part up to 64 chars, up to 8 domain labels, a letter TLD |
| SSN | `123-45-6789` or `123 45 6789` (the same separator twice) |
| Credit card | 13 to 19 digits, with optional single spaces or dashes, that pass the Luhn check |

The Luhn check is the checksum that every card number passes; 9 in 10 order numbers and
other long IDs fail it. Digit look-arounds stop a match inside a longer number. The redactor
keeps the last sent character as context, so the look-arounds decide the same way as for the
text in one piece. A test proves it: for random texts split at random, the streamed output
equals the one-pass output.

**The SSE stream** (`sse.py`):
- `delta.content` and `delta.refusal` are redacted; other fields pass unchanged. One event
  in gives one event out. Each choice and text field has its own redactor.
- `logprobs` is removed: it repeats every token, so it would show the text unredacted.
- Held text is sent with the choice's `finish_reason` event, before `[DONE]`, or at a clean
  end of the stream.
- **Fail closed:** an event that the gateway cannot read is dropped, because it could carry
  PII. If the stream breaks, the held text is dropped (it could be half an email address)
  and an error event ends the stream.

**Proxying.** The request body is forwarded byte for byte, with the gateway's own provider
key (the client's `Authorization` and cookies are not forwarded). One pooled HTTP client
serves all requests. The reply streams with no buffering, and the provider connection closes
when the client leaves.

### Error mapping

| Situation | HTTP | `error.type` |
|---|---|---|
| Body is not a JSON object, or `stream` is not `true` | 400 | `invalid_request_error` |
| Provider cannot be reached | 502 | `provider_error` |
| Provider does not answer in time | 504 | `provider_error` |
| Provider returns an error status | same status | `provider_error` (`LLM provider returned HTTP 429`) |
| Provider reply is not an event stream | 502 | `provider_error` |
| Provider stream breaks, or sends a line over the limit | 200, then an SSE error event | `provider_error` |

The provider's error body is never forwarded, because it can echo the prompt. No message
text is ever logged.

## Tests

80 tests in three seams (a seam is the public boundary that a test calls). Coverage is 100%,
including the gateway subprocess. Tested on Windows 11 with Python 3.12.

**Redactor** ([`tests/test_redactor.py`](tests/test_redactor.py), 38 tests): each PII kind
and look-alikes that are not PII (phone numbers, Luhn-failing numbers); PII split at
**every** pair of chunk boundaries; random splits equal one pass; the 601-character bound;
linear time on a large chunk.

**Gateway HTTP** ([`tests/test_gateway.py`](tests/test_gateway.py), 33 tests). The real app
runs in-process with a recording mock provider. It covers every rule above: SSE split at any
byte, per-choice state, `refusal` and `logprobs`, held text at the end, dropped events, the
line limit, a mid-stream break, forwarding, and every row of the error table.

**Real processes** ([`tests/test_e2e.py`](tests/test_e2e.py), 9 tests). The real gateway and
the mock provider (4 characters every 0.1 s) run as separate processes:
- the client gets the reply with all PII redacted;
- **TTFT:** the first token arrives in under a quarter of the provider's stream time, and
  tokens arrive while the provider still streams;
- the gateway log holds no message text;
- the gateway refuses to start with any of 6 bad settings, with a one-line reason.

## How to run

Prerequisites: Python 3.11+ and [uv](https://docs.astral.sh/uv/). Run every command from the
`task3-pii-guardrail` folder in a bash shell.

```bash
cd task3-pii-guardrail
```

```bash
uv sync
```

| Variable | Default | Meaning |
|---|---|---|
| `GUARDRAIL_PROVIDER_URL` | `http://127.0.0.1:8002/v1/chat/completions` | The LLM provider |
| `GUARDRAIL_PROVIDER_API_KEY` | none | Sent to the provider as a Bearer token |
| `GUARDRAIL_HOST` / `GUARDRAIL_PORT` | `127.0.0.1` / `8000` | Where the gateway listens |
| `GUARDRAIL_PROVIDER_TIMEOUT_SECONDS` | `60` | Longest wait for the provider (also between chunks) |

To use a real provider, set `GUARDRAIL_PROVIDER_URL` to its OpenAI-compatible endpoint and
`GUARDRAIL_PROVIDER_API_KEY` to its key.

### Start the servers

1. In terminal 1, start the mock provider:

   ```bash
   uv run mock-llm-provider
   ```

2. In terminal 2, start the gateway:

   ```bash
   uv run pii-guardrail
   ```

### Try it

The mock provider streams the user message back, 4 characters at a time:

```bash
curl -sN http://127.0.0.1:8000/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"mock","stream":true,"messages":[{"role":"user","content":"Mail jane@example.com, SSN 123-45-6789, card 4111 1111 1111 1111."}]}'
```

The `content` values join to `Mail [REDACTED], SSN [REDACTED], card [REDACTED].`

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

- **OpenAI chat completion stream format**, the most common LLM gateway format. Only
  `stream: true` requests are accepted, because the task is about the stream.
- **Only the generated text is redacted** (`content` and `refusal`). Tool call arguments are
  out of scope.
- **Luhn check on cards** removes most false positives on long IDs. The trade-off: a card
  number with a typo fails the check and is sent unredacted.
- **No client authentication.** The task does not ask for it (Task 2 covers auth).
- **Regex-based detection** finds the three kinds by shape. Names, addresses, and other PII
  need an NER model, which the task does not ask for.
