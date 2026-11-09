"""Seam 1: TokenRateLimiter.reserve() / settle() / release(), on a real SQLite file."""

import sqlite3
from pathlib import Path

import anyio
import pytest

from model_router.limiter import (
    RateLimitExceeded,
    RequestTooLarge,
    TokenRateLimiter,
)

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

LIMIT = 50_000
WINDOW = 60.0
TENANT_A = "tenant-a-key-0123456789"
TENANT_B = "tenant-b-key-0123456789"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "router.db"


@pytest.fixture
def limiter(db_path: Path, clock: FakeClock) -> TokenRateLimiter:
    return TokenRateLimiter(db_path, limit=LIMIT, window_seconds=WINDOW, clock=clock)


# --- Token tracking ----------------------------------------------------------------------


async def test_reservations_count_against_the_limit_until_it_is_full(limiter) -> None:
    first = await limiter.reserve(TENANT_A, 30_000)
    second = await limiter.reserve(TENANT_A, 20_000)
    assert (first.remaining, second.remaining) == (20_000, 0)
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve(TENANT_A, 1)


async def test_a_request_that_does_not_fit_is_refused_and_records_nothing(limiter) -> None:
    await limiter.reserve(TENANT_A, 45_000)
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve(TENANT_A, 6_000)
    assert (await limiter.reserve(TENANT_A, 5_000)).remaining == 0


async def test_settle_replaces_the_estimate_with_the_real_usage(limiter) -> None:
    reservation = await limiter.reserve(TENANT_A, 40_000)
    await limiter.settle(reservation, 1_000)
    assert (await limiter.reserve(TENANT_A, 9_000)).remaining == 40_000


async def test_release_refunds_a_reservation(limiter) -> None:
    reservation = await limiter.reserve(TENANT_A, 50_000)
    await limiter.release(reservation)
    assert (await limiter.reserve(TENANT_A, 50_000)).remaining == 0


async def test_tenants_have_separate_budgets(limiter) -> None:
    await limiter.reserve(TENANT_A, 50_000)
    assert (await limiter.reserve(TENANT_B, 50_000)).remaining == 0


@pytest.mark.parametrize("tokens", [LIMIT + 1, 10**9])
async def test_a_request_larger_than_the_whole_limit_is_too_large(limiter, tokens: int) -> None:
    with pytest.raises(RequestTooLarge):
        await limiter.reserve(TENANT_A, tokens)


@pytest.mark.parametrize("tokens", [0, -5])
async def test_a_reservation_needs_a_positive_token_count(limiter, tokens: int) -> None:
    with pytest.raises(ValueError):
        await limiter.reserve(TENANT_A, tokens)


# --- The sliding window ------------------------------------------------------------------


async def test_tokens_leave_the_window_exactly_after_the_window_length(limiter, clock) -> None:
    await limiter.reserve(TENANT_A, 50_000)
    clock.advance(WINDOW - 0.5)  # halves are exact in binary floating point
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve(TENANT_A, 1)
    clock.advance(0.5)
    assert (await limiter.reserve(TENANT_A, 50_000)).remaining == 0


async def test_the_window_slides_one_reservation_at_a_time(limiter, clock) -> None:
    await limiter.reserve(TENANT_A, 20_000)  # t = 0
    clock.advance(20)
    await limiter.reserve(TENANT_A, 20_000)  # t = 20
    clock.advance(20)
    await limiter.reserve(TENANT_A, 10_000)  # t = 40: the window is full
    clock.advance(20)  # t = 60: the first 20,000 leave
    assert (await limiter.reserve(TENANT_A, 20_000)).remaining == 0
    with pytest.raises(RateLimitExceeded):
        await limiter.reserve(TENANT_A, 1)


async def test_retry_after_is_the_time_until_enough_tokens_leave(limiter, clock) -> None:
    await limiter.reserve(TENANT_A, 20_000)  # t = 0, leaves at 60
    clock.advance(10)
    await limiter.reserve(TENANT_A, 20_000)  # t = 10, leaves at 70
    clock.advance(5)
    await limiter.reserve(TENANT_A, 10_000)  # t = 15, leaves at 75
    clock.advance(15)  # t = 30
    with pytest.raises(RateLimitExceeded) as small:
        await limiter.reserve(TENANT_A, 15_000)  # the first reservation is enough
    with pytest.raises(RateLimitExceeded) as large:
        await limiter.reserve(TENANT_A, 35_000)  # the first two are needed
    assert small.value.retry_after_seconds == pytest.approx(30)
    assert large.value.retry_after_seconds == pytest.approx(40)


async def test_expired_rows_are_deleted_from_the_database(limiter, clock, db_path) -> None:
    for _ in range(5):
        await limiter.reserve(TENANT_A, 1_000)
    clock.advance(WINDOW)
    await limiter.reserve(TENANT_A, 1_000)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM token_usage").fetchone() == (1,)


async def test_expired_rows_of_an_idle_tenant_are_deleted_too(limiter, clock, db_path) -> None:
    await limiter.reserve(TENANT_B, 1_000)  # tenant B never sends another request
    clock.advance(WINDOW)
    await limiter.reserve(TENANT_A, 1_000)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM token_usage").fetchone() == (1,)


# --- Concurrency and storage -------------------------------------------------------------


async def test_concurrent_reservations_never_overspend(limiter) -> None:
    accepted: list[int] = []
    refused: list[int] = []

    async def try_reserve() -> None:
        try:
            accepted.append((await limiter.reserve(TENANT_A, 1_000)).remaining)
        except RateLimitExceeded:
            refused.append(1)

    async with anyio.create_task_group() as group:
        for _ in range(120):
            group.start_soon(try_reserve)
    assert (len(accepted), len(refused)) == (50, 70)
    assert sorted(accepted) == list(range(0, 50_000, 1_000))


async def test_usage_survives_a_new_limiter_on_the_same_file(db_path, clock) -> None:
    first = TokenRateLimiter(db_path, limit=LIMIT, window_seconds=WINDOW, clock=clock)
    await first.reserve(TENANT_A, 50_000)
    restarted = TokenRateLimiter(db_path, limit=LIMIT, window_seconds=WINDOW, clock=clock)
    with pytest.raises(RateLimitExceeded):
        await restarted.reserve(TENANT_A, 1)


async def test_the_database_never_holds_the_raw_api_key(limiter, db_path) -> None:
    await limiter.reserve(TENANT_A, 10)
    assert TENANT_A.encode() not in db_path.read_bytes()
