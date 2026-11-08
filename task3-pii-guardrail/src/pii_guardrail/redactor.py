"""Incremental PII redaction over a stream of text chunks.

The redactor sends text on as soon as no PII match can still cover it. It holds back only
the tail that could still grow into a match, so the held text is bounded by the longest
possible match and the first token is not delayed by the rest of the stream.
"""

import regex

REDACTED = "[REDACTED]"

# Every quantifier is bounded, so every match (and every partial match) has a maximum length.
EMAIL_LOCAL_MAX = 64
EMAIL_LABEL_MAX = 63
EMAIL_LABELS_MAX = 8
EMAIL_TLD_MAX = 24
EMAIL = (
    rf"[\w.%+-]{{1,{EMAIL_LOCAL_MAX}}}@"
    rf"(?:[A-Za-z0-9-]{{1,{EMAIL_LABEL_MAX}}}\.){{1,{EMAIL_LABELS_MAX}}}"
    rf"[A-Za-z]{{2,{EMAIL_TLD_MAX}}}"
)
# 13 to 19 digits, with an optional single space or dash between digits. A match is a card
# number only if it also passes the Luhn check (see _is_card_number).
CARD = r"(?P<card>(?<!\d)(?:\d[ -]?){12,18}\d(?!\d))"
# 3-2-4 digits, with the same separator (dash or space) twice.
SSN = r"(?<!\d)\d{3}(?P<sep>[- ])\d{2}(?P=sep)\d{4}(?!\d)"

# SSN comes before CARD, so a longer digit run that fails the Luhn check cannot hide an SSN.
_ANY_PII = f"{EMAIL}|{SSN}|{CARD}"
PII_PATTERN = regex.compile(_ANY_PII)
# The same pattern, but it can never complete. A partial search with it returns the leftmost
# position where the rest of the text is the start of a PII match: the point to hold from.
# (`regex` reports a complete match before an earlier partial one, so PII_PATTERN alone
# cannot find this point.)
_POSSIBLE_MATCH = regex.compile(f"(?:{_ANY_PII})(?!)")

# The longest possible match: an email address (the "@" and each label dot add one char).
MAX_HELD_CHARS = EMAIL_LOCAL_MAX + 1 + EMAIL_LABELS_MAX * (EMAIL_LABEL_MAX + 1) + EMAIL_TLD_MAX


class StreamRedactor:
    """Redact PII in a text stream that arrives in chunks of any size.

    Call feed() for each chunk and send its return value at once. Call flush() once at the
    normal end of the stream. On an abnormal end, do not call flush(): the held tail could be
    the first part of PII.
    """

    def __init__(self) -> None:
        self._held = ""
        # The last character already sent. The look-behinds (?<!\d) need it to decide a match
        # at the start of the held text in the same way as for the text in one piece.
        self._context = ""

    def feed(self, chunk: str) -> str:
        return self._redact(self._held + chunk, is_final=False)

    def flush(self) -> str:
        return self._redact(self._held, is_final=True)

    def _redact(self, held_and_new: str, *, is_final: bool) -> str:
        """Send all text before the hold point, with each complete match replaced.

        The hold point is found once per call, so a call costs time in proportion to its
        text. It moves only when a match runs past it, which needs a new search from there.
        """
        text = self._context + held_and_new
        cursor = len(self._context)
        hold_at = len(text) if is_final else _hold_point(text, cursor)
        sent: list[str] = []
        search_from = cursor
        while match := PII_PATTERN.search(text, pos=search_from):
            if match.start() >= hold_at:
                break
            if match.group("card") is not None and not _is_card_number(match.group()):
                # Card-shaped digits that are not a card number stay as they are. Search again
                # from the next character, so PII that starts inside the digits is still found.
                search_from = match.start() + 1
                continue
            sent += [text[cursor : match.start()], REDACTED]
            cursor = search_from = match.end()
            if cursor > hold_at:
                hold_at = _hold_point(text, cursor)
        sent.append(text[cursor:hold_at])
        self._held = text[hold_at:]
        self._context = "" if is_final else text[max(hold_at - 1, 0) : hold_at]
        return "".join(sent)


def _hold_point(text: str, pos: int) -> int:
    """Return the first index at or after pos where more input could still make a match."""
    possible = _POSSIBLE_MATCH.search(text, pos=pos, partial=True)
    return len(text) if possible is None else possible.start()


def _is_card_number(candidate: str) -> bool:
    """The Luhn check: every card number passes it, and 9 in 10 random digit runs fail it."""
    total = 0
    for position, char in enumerate(reversed([c for c in candidate if c.isdigit()])):
        digit = int(char)
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0
