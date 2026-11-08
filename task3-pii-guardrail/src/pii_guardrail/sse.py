"""Redact the delta text in an OpenAI-style chat completion SSE stream, event by event."""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from pii_guardrail.redactor import StreamRedactor

logger = logging.getLogger(__name__)

DATA_PREFIX = "data:"
DONE = "[DONE]"
STREAM_FAILED_EVENT = {"error": {"message": "LLM provider stream failed", "type": "provider_error"}}
# The longest SSE line the gateway reads. A normal chunk line is far shorter; the limit stops a
# provider line without an end from filling memory.
MAX_LINE_CHARS = 1_048_576
# The delta fields that carry generated text. Each one streams in parts, so each one has its
# own redactor per choice.
TEXT_FIELDS = ("content", "refusal")
# Choice fields that repeat the generated text outside the delta, so they are removed.
# `logprobs` lists every token, and a client could ask for it to read the text unredacted.
REMOVED_CHOICE_FIELDS = ("logprobs",)
# Chunk fields that are copied onto the extra events that send the held text at the end.
ENVELOPE_FIELDS = ("id", "object", "created", "model")


class LineTooLongError(Exception):
    """The provider sent an SSE line longer than MAX_LINE_CHARS."""


async def read_lines(text: AsyncIterator[str]) -> AsyncIterator[str]:
    """Split decoded text into SSE lines (ended by LF, CR LF, or CR), with a length limit."""
    pending = ""
    async for piece in text:
        # A CR LF split across two pieces gives an extra blank line, which SSE ignores.
        pending += piece.replace("\r\n", "\n").replace("\r", "\n")
        *lines, pending = pending.split("\n")
        for line in lines:
            yield line
        if len(pending) > MAX_LINE_CHARS:
            raise LineTooLongError
    if pending:
        yield pending


async def redact_events(text: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield the provider's events with the generated text redacted.

    A choice's held text is sent with its `finish_reason` event, or before `[DONE]`, or at a
    clean end of the stream. If the provider stream fails, the held text is dropped (it could
    be the start of PII) and an error event ends the stream.
    """
    choices = _ChoiceRedactors()
    try:
        async for line in read_lines(text):
            if not line.startswith(DATA_PREFIX):
                continue  # blank separators, comments, and event:/id: fields
            data = line.removeprefix(DATA_PREFIX).strip()
            if data == DONE:
                for event in choices.flush():
                    yield event
                yield _event(DONE)
                return
            chunk = _parse_chunk(data)
            if chunk is not None:
                yield _event(choices.redact(chunk))
    except (httpx.TransportError, LineTooLongError) as exc:
        logger.warning("provider stream failed: %s", type(exc).__name__)
        yield _event(STREAM_FAILED_EVENT)
        return
    for event in choices.flush():
        yield event


def _event(payload: object) -> str:
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {data}\n\n"


def _parse_chunk(data: str) -> dict[str, Any] | None:
    """Return the event as a chunk with a `choices` list, or None to drop it.

    Fail closed: an event that the gateway cannot read could carry PII, so it is not sent.
    """
    try:
        chunk = json.loads(data)
    except ValueError:
        chunk = None
    if not isinstance(chunk, dict) or not isinstance(chunk.get("choices"), list):
        logger.warning("dropped a provider event that is not a chat completion chunk")
        return None
    return chunk


def _is_choice(choice: object) -> bool:
    if not isinstance(choice, dict):
        return False
    index = choice.get("index", 0)
    return isinstance(index, int) and not isinstance(index, bool)


class _ChoiceRedactors:
    """The redaction state of one stream: a redactor per (choice index, text field)."""

    def __init__(self) -> None:
        self._redactors: dict[tuple[int, str], StreamRedactor] = {}
        self._envelope: dict[str, Any] = {"object": "chat.completion.chunk"}

    def redact(self, chunk: dict[str, Any]) -> dict[str, Any]:
        self._envelope |= {k: chunk[k] for k in ENVELOPE_FIELDS if k in chunk}
        choices = [self._redact_choice(c) for c in chunk["choices"] if _is_choice(c)]
        return {**chunk, "choices": choices}

    def flush(self) -> list[str]:
        """Return one event for each held text, at the end of the stream."""
        events = []
        for (index, field), redactor in self._redactors.items():
            tail = redactor.flush()
            if tail:
                choice = {"index": index, "delta": {field: tail}, "finish_reason": None}
                events.append(_event({**self._envelope, "choices": [choice]}))
        self._redactors.clear()
        return events

    def _redact_choice(self, choice: dict[str, Any]) -> dict[str, Any]:
        index = choice.get("index", 0)
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        is_finished = choice.get("finish_reason") is not None
        redacted = {}
        for field in TEXT_FIELDS:
            text = self._redact_field(index, field, delta.get(field), is_finished)
            if text is not None:
                redacted[field] = text
        kept = {k: v for k, v in choice.items() if k not in REMOVED_CHOICE_FIELDS}
        if not redacted:
            return kept
        return {**kept, "delta": {**delta, **redacted}}

    def _redact_field(self, index: int, field: str, value: object, is_finished: bool) -> str | None:
        """Return the text to send for one field, or None when the field stays absent."""
        key = (index, field)
        if isinstance(value, str):
            text = self._redactors.setdefault(key, StreamRedactor()).feed(value)
        elif key in self._redactors and is_finished:
            text = ""
        else:
            return None
        if is_finished:
            text += self._redactors.pop(key).flush()
            return text if text or isinstance(value, str) else None
        return text
