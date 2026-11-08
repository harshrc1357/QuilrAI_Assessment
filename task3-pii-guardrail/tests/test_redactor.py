"""Seam 1: StreamRedactor.feed() / flush(), the incremental redaction core."""

import random
import time

import pytest

from pii_guardrail.redactor import MAX_HELD_CHARS, REDACTED, StreamRedactor

pytestmark = pytest.mark.unit

# A generous bound: the linear redactor needs well under 0.1 s for these inputs.
LARGE_CHUNK_SECONDS = 2.0


def redact_in_chunks(chunks: list[str]) -> str:
    redactor = StreamRedactor()
    return "".join(redactor.feed(chunk) for chunk in chunks) + redactor.flush()


def redact_whole(text: str) -> str:
    return redact_in_chunks([text])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mail jane.doe+news@example.co.uk now", "Mail [REDACTED] now"),
        ("Mail JOHN_99@sub.mail-host.io.", "Mail [REDACTED]."),
        ("Mail a@mail.dept.corp.example.co.uk ok", "Mail [REDACTED] ok"),  # 6 domain labels
        ("SSN 123-45-6789 on file", "SSN [REDACTED] on file"),
        ("SSN 123 45 6789 on file", "SSN [REDACTED] on file"),
        ("SSN 123-45-6789-1234 x", "SSN [REDACTED]-1234 x"),  # SSN inside a non-card run
        ("Card 4111 1111 1111 1111 ok", "Card [REDACTED] ok"),
        ("Card 4111-1111-1111-1111 ok", "Card [REDACTED] ok"),
        ("Card 4111111111111111 ok", "Card [REDACTED] ok"),
        ("Amex 3782 822463 10005 ok", "Amex [REDACTED] ok"),
        ("Card 6011000990139424124 ok", "Card [REDACTED] ok"),  # 19 digits
        ("a@b.io, 123-45-6789 and 5500005555555559", "[REDACTED], [REDACTED] and [REDACTED]"),
    ],
)
def test_redacts_each_pii_kind(text: str, expected: str) -> None:
    assert redact_whole(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Call 555-1234 or 555 123 4567.",  # phone numbers
        "Order 123456789012 shipped",  # 12 digits: too short for a card
        "Id 12345678901234567890 ok",  # 20 digits: too long for a card
        "Order 1234 5678 9012 3456 ok",  # card shape, but fails the Luhn check
        "Card 4111 1111 1111 1112 ok",  # one digit off a valid card: fails Luhn
        "SSN-like 123-456-789 and 12-345-6789",  # wrong grouping
        "Write a @ b.com or user@localhost",  # not an email address
        "Plain text, no secrets at all.",
        "",
    ],
)
def test_leaves_text_without_pii_unchanged(text: str) -> None:
    assert redact_whole(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Reach me at jane.doe@example.com today.", "Reach me at [REDACTED] today."),
        ("SSN 123-45-6789!", "SSN [REDACTED]!"),
        ("Card 4111 1111 1111 1111 3 x", "Card [REDACTED] x"),  # 17 digits
        ("Pay 4111 1111 1111 9 then", "Pay [REDACTED] then"),  # 13 digits
        ("Mail a@b.com.uk end", "Mail [REDACTED] end"),
    ],
)
def test_pii_split_at_every_chunk_boundary_is_redacted(text: str, expected: str) -> None:
    for first in range(len(text) + 1):
        for second in range(first, len(text) + 1):
            chunks = [text[:first], text[first:second], text[second:]]
            assert redact_in_chunks(chunks) == expected, chunks


def test_one_character_chunks_match_whole_text() -> None:
    text = "Hi, I am Jo (jo@x.org), SSN 078-05-1120, card 4012 8888 8888 1881."
    expected = "Hi, I am Jo ([REDACTED]), SSN [REDACTED], card [REDACTED]."
    assert redact_in_chunks(list(text)) == expected


def test_random_chunking_gives_the_same_output_as_one_pass() -> None:
    fragments = [
        "a", "Zed", " ", "-", ".", "@", "1", "42", "123-45-6789", "4111 1111 1111 1111",
        "bob@mail.com", "x.y@a-b.co", "1234 5678 9012 3456", "\n", ", ", "9", "5500 0000 0000 0004",
    ]  # fmt: skip
    rng = random.Random(1234)
    for _ in range(300):
        text = "".join(rng.choice(fragments) for _ in range(rng.randint(0, 40)))
        cuts = sorted(rng.sample(range(len(text) + 1), k=min(len(text) + 1, rng.randint(0, 8))))
        chunks = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)], strict=True)]
        assert redact_in_chunks(chunks) == redact_whole(text), (text, chunks)


def test_text_without_a_possible_match_passes_through_at_once() -> None:
    redactor = StreamRedactor()
    assert redactor.feed("Hello there, ") == "Hello there, "
    assert redactor.feed("how are you? ") == "how are you? "


def test_only_a_possible_partial_match_is_held_back() -> None:
    redactor = StreamRedactor()
    assert redactor.feed("Write to jane.d") == "Write to "
    assert redactor.feed("oe@example.com and") == f"{REDACTED} "
    assert redactor.flush() == "and"


def test_flush_releases_held_text_that_never_became_pii() -> None:
    redactor = StreamRedactor()
    assert redactor.feed("Total 4111 11") == "Total "
    assert redactor.flush() == "4111 11"
    assert redactor.flush() == ""


@pytest.mark.parametrize("run", ["x" * 10_000, "7" * 10_000, "a.b-" * 2_500])
def test_held_text_stays_bounded_for_a_long_unbroken_run(run: str) -> None:
    redactor = StreamRedactor()
    sent = 0
    for index, char in enumerate(run, start=1):
        sent += len(redactor.feed(char))
        assert index - sent <= MAX_HELD_CHARS
    assert sent + len(redactor.flush()) == len(run)


@pytest.mark.parametrize(
    "chunk",
    [
        " ".join(["123-45-6789"] * 2_000) + " x",  # 2,000 matches in one chunk
        " ".join(["1234"] * 5_000),  # a table of short numbers
        " ".join(["a@b.io"] * 3_000),
    ],
)
def test_a_large_chunk_with_many_matches_is_redacted_in_linear_time(chunk: str) -> None:
    started = time.perf_counter()
    redactor = StreamRedactor()
    output = redactor.feed(chunk) + redactor.flush()
    assert time.perf_counter() - started < LARGE_CHUNK_SECONDS
    assert "@" not in output and "-45-" not in output


def test_a_luhn_failing_number_split_across_chunks_passes_unchanged() -> None:
    text = "Ref 1234 5678 9012 3456 and card 4111 1111 1111 1111 end"
    expected = "Ref 1234 5678 9012 3456 and card [REDACTED] end"
    for cut in range(len(text) + 1):
        assert redact_in_chunks([text[:cut], text[cut:]]) == expected, cut
