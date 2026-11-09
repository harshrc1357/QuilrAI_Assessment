"""A token-aware sliding-window rate limiter, stored in an on-disk SQLite database.

Each request reserves its estimated tokens before the provider call. The check and the
reservation run in one write transaction (BEGIN IMMEDIATE), so concurrent requests, also
from other processes on the same file, cannot both pass a check that only one of them fits.
After the call, settle() replaces the estimate with the real usage, or release() refunds it.
"""

import hashlib
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import anyio

# How long a write waits for another writer before SQLite gives up.
BUSY_TIMEOUT_SECONDS = 10.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY,
    tenant TEXT NOT NULL,
    tokens INTEGER NOT NULL,
    reserved_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS token_usage_by_tenant ON token_usage (tenant, reserved_at);
CREATE INDEX IF NOT EXISTS token_usage_by_time ON token_usage (reserved_at);
"""


class RateLimitExceeded(Exception):
    """The tenant's window has no room for the request now."""

    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(f"retry after {retry_after_seconds:.3f} s")
        self.retry_after_seconds = retry_after_seconds


class RequestTooLarge(Exception):
    """The request needs more tokens than the whole limit, so it can never fit."""


@dataclass(frozen=True)
class Reservation:
    id: int
    remaining: int  # tokens left in the tenant's window after this reservation


class TokenRateLimiter:
    def __init__(
        self,
        db_path: Path,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # The wall clock, not a monotonic one: the rows are shared with other processes.
        self._db_path = db_path
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    async def reserve(self, api_key: str, tokens: int) -> Reservation:
        if tokens <= 0:
            raise ValueError("tokens must be positive")
        if tokens > self._limit:
            raise RequestTooLarge
        return await anyio.to_thread.run_sync(self._reserve, _tenant_id(api_key), tokens)

    async def settle(self, reservation: Reservation, tokens: int) -> None:
        await self._write("UPDATE token_usage SET tokens = ? WHERE id = ?", tokens, reservation.id)

    async def release(self, reservation: Reservation) -> None:
        await self._write("DELETE FROM token_usage WHERE id = ?", reservation.id)

    def _reserve(self, tenant: str, tokens: int) -> Reservation:
        now = self._clock()
        window_start = now - self._window
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                # Eviction: rows that left the window are deleted for every tenant, so the
                # table stays small also when a tenant stops sending requests.
                db.execute("DELETE FROM token_usage WHERE reserved_at <= ?", (window_start,))
                rows = db.execute(
                    "SELECT tokens, reserved_at FROM token_usage WHERE tenant = ?"
                    " ORDER BY reserved_at",
                    (tenant,),
                ).fetchall()
                used = sum(row_tokens for row_tokens, _ in rows)
                if used + tokens > self._limit:
                    raise RateLimitExceeded(self._retry_after(rows, used + tokens, now))
                cursor = db.execute(
                    "INSERT INTO token_usage (tenant, tokens, reserved_at) VALUES (?, ?, ?)",
                    (tenant, tokens, now),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return Reservation(id=cursor.lastrowid, remaining=self._limit - used - tokens)

    def _retry_after(self, rows: list[tuple[int, float]], needed: int, now: float) -> float:
        """Seconds until the oldest rows that must leave for the request to fit have left.

        The request is never larger than the limit, so the rows always free enough.
        """
        excess = needed - self._limit
        freed = 0
        for row_tokens, reserved_at in rows:  # noqa: B007 (reserved_at is used after the loop)
            freed += row_tokens
            if freed >= excess:
                break
        return reserved_at + self._window - now

    async def _write(self, sql: str, *params: object) -> None:
        def run() -> None:
            with closing(self._connect()) as db:
                db.execute(sql, params)

        await anyio.to_thread.run_sync(run)

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: no implicit transactions; each statement commits on its own
        # unless an explicit BEGIN is open.
        return sqlite3.connect(self._db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)


def _tenant_id(api_key: str) -> str:
    """The database stores a hash of the API key, never the key itself."""
    return hashlib.sha256(api_key.encode()).hexdigest()
