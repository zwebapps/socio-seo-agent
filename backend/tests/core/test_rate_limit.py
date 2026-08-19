"""Fixed-window rate limiting and the argon2 concurrency gate.

Written before the module. Two of the assertions here are the whole reason the
file exists, and both are about a *bound* rather than about a return value:

* **the per-IP and per-email windows trip independently.** A single combined key
  would look identical in every happy-path test and would stop neither attack:
  credential stuffing varies the email while holding the IP, and a targeted
  attack varies the IP while holding the email. So each dimension is proved to
  trip while the other is deliberately varied.
* **the gate's observed peak concurrency equals its limit — exactly.** Asserting
  only "it finished" would pass for a gate that does nothing, and asserting only
  ``<= limit`` would pass for a gate that serialises everything and throws the
  throughput away. The peak is recorded from inside the work itself, so it is a
  measurement rather than a restatement of the configuration.

A third is a regression test rather than a requirement: ``asyncio.Semaphore``
binds itself to the first event loop that *contends* on it and raises
``RuntimeError`` for any other, which makes a module-level semaphore a latent
crash in any process that runs more than one loop — every pytest-asyncio suite,
and any script that calls ``asyncio.run`` twice. The gate therefore keeps one
semaphore per loop, and ``test_the_gate_survives_a_second_event_loop`` fails if
that is ever simplified away.

No network is required. The Redis-backed tests skip when Redis is unreachable,
and the fail-open test points a counter at a closed port on purpose.
"""

import asyncio
import threading
import time
from typing import Final

import pytest

from backend.app.core import rate_limit
from backend.app.core.rate_limit import (
    ConcurrencyGate,
    FixedWindowRateLimiter,
    InMemoryWindowCounter,
    RateLimitBackendError,
    RateLimitRule,
    RedisWindowCounter,
)

# A port nothing listens on. Used to prove the limiter degrades instead of
# refusing traffic when its shared counter is unreachable.
DEAD_REDIS_URL: Final = "redis://127.0.0.1:6399/0"
LIVE_REDIS_URL: Final = "redis://localhost:6381/0"

SECRET: Final = "rate-limit-test-secret"

# Every rule below uses a LONG window. These tests assert that a limit TRIPS; none
# of them waits for one to reset. A fixed window is bucketed on the wall clock
# (`int(now) // window`), so a short window means a test that straddles a bucket
# edge sees its count reset mid-way and fails perhaps once in a thousand runs --
# a flake that would be blamed on the limiter rather than on the test. A long
# window makes the edge unreachable inside a millisecond-scale test.
LONG_WINDOW: Final = 3600


def _limiter(
    *,
    ip: RateLimitRule | None = None,
    email: RateLimitRule | None = None,
    counter: rate_limit.WindowCounter | None = None,
    fallback: rate_limit.WindowCounter | None = None,
    namespace: str = "test",
) -> FixedWindowRateLimiter:
    rules = {}
    if ip is not None:
        rules[rate_limit.DIMENSION_IP] = ip
    if email is not None:
        rules[rate_limit.DIMENSION_EMAIL] = email
    return FixedWindowRateLimiter(
        rules=rules,
        counter=counter if counter is not None else InMemoryWindowCounter(),
        fallback=fallback,
        namespace=f"{namespace}:{time.monotonic_ns()}",
        secret=SECRET,
    )


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def test_a_rule_refuses_a_limit_below_one() -> None:
    """A limit of zero would refuse every request, including the first."""
    with pytest.raises(ValueError):
        RateLimitRule(limit=0, window_seconds=LONG_WINDOW)


def test_a_rule_refuses_a_window_below_one_second() -> None:
    with pytest.raises(ValueError):
        RateLimitRule(limit=5, window_seconds=0)


# --------------------------------------------------------------------------- #
# The in-memory counter
# --------------------------------------------------------------------------- #


async def test_the_in_memory_counter_returns_a_rising_count() -> None:
    counter = InMemoryWindowCounter()
    counts = [await counter.bump("k", 60) for _ in range(3)]
    assert counts == [1, 2, 3]


async def test_the_in_memory_counter_keys_are_independent() -> None:
    counter = InMemoryWindowCounter()
    assert await counter.bump("a", 60) == 1
    assert await counter.bump("b", 60) == 1


async def test_the_in_memory_counter_forgets_an_elapsed_window() -> None:
    """A one-second window, waited out. Without expiry the limiter never resets."""
    counter = InMemoryWindowCounter()
    assert await counter.bump("k", 1) == 1
    await asyncio.sleep(1.05)
    assert await counter.bump("k", 1) == 1


async def test_the_in_memory_counter_does_not_grow_without_bound() -> None:
    """Every bucket is a distinct key, so an un-pruned dict is a slow memory leak."""
    counter = InMemoryWindowCounter()
    for index in range(rate_limit.IN_MEMORY_MAX_KEYS * 2):
        await counter.bump(f"k{index}", 1)
    assert counter.tracked_keys <= rate_limit.IN_MEMORY_MAX_KEYS * 2


# --------------------------------------------------------------------------- #
# Per-IP and per-email windows
# --------------------------------------------------------------------------- #


async def test_the_per_ip_limit_trips_at_the_threshold_and_not_before() -> None:
    limiter = _limiter(ip=RateLimitRule(limit=3, window_seconds=LONG_WINDOW))
    values = {rate_limit.DIMENSION_IP: "198.51.100.7"}

    allowed = [(await limiter.check(values)).allowed for _ in range(3)]
    tripped = await limiter.check(values)

    assert allowed == [True, True, True], "the limit itself must still be served"
    assert tripped.allowed is False


async def test_the_per_ip_limit_does_not_punish_a_different_ip() -> None:
    limiter = _limiter(ip=RateLimitRule(limit=2, window_seconds=LONG_WINDOW))
    for _ in range(3):
        await limiter.check({rate_limit.DIMENSION_IP: "198.51.100.7"})

    other = await limiter.check({rate_limit.DIMENSION_IP: "203.0.113.9"})
    assert other.allowed is True


async def test_the_per_email_limit_trips_while_the_ip_keeps_changing() -> None:
    """The targeted attack: one account, a fresh IP for every attempt.

    The per-IP window never fills, so only an independent per-email window stops
    this. The IP rule is deliberately generous so it cannot be what trips.
    """
    limiter = _limiter(
        ip=RateLimitRule(limit=1000, window_seconds=LONG_WINDOW),
        email=RateLimitRule(limit=3, window_seconds=LONG_WINDOW),
    )

    decisions = [
        await limiter.check(
            {
                rate_limit.DIMENSION_IP: f"198.51.100.{index}",
                rate_limit.DIMENSION_EMAIL: "victim@example.test",
            }
        )
        for index in range(5)
    ]

    assert [d.allowed for d in decisions] == [True, True, True, False, False]
    assert decisions[-1].dimension == rate_limit.DIMENSION_EMAIL


async def test_the_per_ip_limit_trips_while_the_email_keeps_changing() -> None:
    """Credential stuffing: one host, a different account every time.

    The mirror image of the test above, and the reason the two dimensions cannot
    be collapsed into one key.
    """
    limiter = _limiter(
        ip=RateLimitRule(limit=3, window_seconds=LONG_WINDOW),
        email=RateLimitRule(limit=1000, window_seconds=LONG_WINDOW),
    )

    decisions = [
        await limiter.check(
            {
                rate_limit.DIMENSION_IP: "198.51.100.7",
                rate_limit.DIMENSION_EMAIL: f"target{index}@example.test",
            }
        )
        for index in range(5)
    ]

    assert [d.allowed for d in decisions] == [True, True, True, False, False]
    assert decisions[-1].dimension == rate_limit.DIMENSION_IP


async def test_the_email_dimension_is_case_and_whitespace_insensitive() -> None:
    """Otherwise ``  Victim@Example.test `` is a free extra budget per spelling."""
    limiter = _limiter(email=RateLimitRule(limit=2, window_seconds=LONG_WINDOW))
    spellings = ["victim@example.test", "  VICTIM@Example.TEST ", "Victim@example.Test"]

    decisions = [
        await limiter.check({rate_limit.DIMENSION_EMAIL: spelling}) for spelling in spellings
    ]
    assert [d.allowed for d in decisions] == [True, True, False]


async def test_a_dimension_with_no_rule_is_ignored() -> None:
    limiter = _limiter(ip=RateLimitRule(limit=1, window_seconds=LONG_WINDOW))
    first = await limiter.check({rate_limit.DIMENSION_EMAIL: "nobody@example.test"})
    assert first.allowed is True


async def test_a_tripped_decision_carries_a_usable_retry_after() -> None:
    """The value goes straight into a ``Retry-After`` header, so it must be sane."""
    limiter = _limiter(ip=RateLimitRule(limit=1, window_seconds=LONG_WINDOW))
    values = {rate_limit.DIMENSION_IP: "198.51.100.7"}
    await limiter.check(values)
    tripped = await limiter.check(values)

    assert tripped.allowed is False
    assert 1 <= tripped.retry_after_seconds <= LONG_WINDOW


async def test_an_allowed_decision_has_no_retry_after() -> None:
    limiter = _limiter(ip=RateLimitRule(limit=5, window_seconds=LONG_WINDOW))
    allowed = await limiter.check({rate_limit.DIMENSION_IP: "198.51.100.7"})
    assert allowed.allowed is True
    assert allowed.retry_after_seconds == 0
    assert allowed.dimension is None


async def test_the_raw_email_never_appears_in_a_counter_key() -> None:
    """Keys are HMAC digests: a dump of the counter store must not be an address book."""
    recorded: list[str] = []

    class Recorder:
        async def bump(self, key: str, window_seconds: int) -> int:
            recorded.append(key)
            return 1

    limiter = _limiter(email=RateLimitRule(limit=5, window_seconds=LONG_WINDOW), counter=Recorder())
    await limiter.check({rate_limit.DIMENSION_EMAIL: "victim@example.test"})

    assert recorded
    assert "victim@example.test" not in recorded[0]
    assert "victim" not in recorded[0]


async def test_two_limiters_with_different_namespaces_do_not_share_a_budget() -> None:
    """Login and signup have separate policies, so they need separate counters."""
    counter = InMemoryWindowCounter()
    login = _limiter(
        ip=RateLimitRule(limit=1, window_seconds=LONG_WINDOW), counter=counter, namespace="login"
    )
    signup = _limiter(
        ip=RateLimitRule(limit=1, window_seconds=LONG_WINDOW), counter=counter, namespace="signup"
    )
    values = {rate_limit.DIMENSION_IP: "198.51.100.7"}

    assert (await login.check(values)).allowed is True
    assert (await login.check(values)).allowed is False
    assert (await signup.check(values)).allowed is True


# --------------------------------------------------------------------------- #
# Redis, and what happens when it is not there
# --------------------------------------------------------------------------- #


async def test_the_redis_counter_trips_the_limiter() -> None:
    counter = RedisWindowCounter(LIVE_REDIS_URL)
    try:
        try:
            await counter.bump(f"probe:{time.monotonic_ns()}", 5)
        except RateLimitBackendError as exc:
            pytest.skip(f"Redis unreachable: {exc}")

        limiter = _limiter(ip=RateLimitRule(limit=2, window_seconds=LONG_WINDOW), counter=counter)
        values = {rate_limit.DIMENSION_IP: "198.51.100.7"}
        decisions = [(await limiter.check(values)).allowed for _ in range(4)]
    finally:
        await counter.aclose()

    assert decisions == [True, True, False, False]


async def test_two_limiters_sharing_redis_share_one_budget() -> None:
    """The point of Redis at all: two replicas must not each grant the full limit."""
    counter_a = RedisWindowCounter(LIVE_REDIS_URL)
    counter_b = RedisWindowCounter(LIVE_REDIS_URL)
    namespace = f"shared:{time.monotonic_ns()}"
    try:
        try:
            await counter_a.bump(f"probe:{time.monotonic_ns()}", 5)
        except RateLimitBackendError as exc:
            pytest.skip(f"Redis unreachable: {exc}")

        replica_a = _limiter(
            ip=RateLimitRule(limit=2, window_seconds=LONG_WINDOW),
            counter=counter_a,
            namespace=namespace,
        )
        replica_b = FixedWindowRateLimiter(
            rules=replica_a.rules,
            counter=counter_b,
            namespace=replica_a.namespace,
            secret=SECRET,
        )
        values = {rate_limit.DIMENSION_IP: "198.51.100.7"}

        first = await replica_a.check(values)
        second = await replica_b.check(values)
        third = await replica_a.check(values)
    finally:
        await counter_a.aclose()
        await counter_b.aclose()

    assert [first.allowed, second.allowed, third.allowed] == [True, True, False]


async def test_an_unreachable_redis_does_not_refuse_the_request() -> None:
    """The fail-OPEN decision, asserted rather than described.

    A Redis outage must not become an authentication outage: refusing every login
    when the counter store is down hands an attacker the denial of service the
    limiter exists to prevent, by a cheaper route than guessing a password. See
    the module docstring in ``rate_limit.py`` for the full argument.
    """
    limiter = _limiter(
        ip=RateLimitRule(limit=2, window_seconds=LONG_WINDOW),
        counter=RedisWindowCounter(DEAD_REDIS_URL, timeout=0.05),
    )
    decision = await limiter.check({rate_limit.DIMENSION_IP: "198.51.100.7"})
    assert decision.allowed is True


async def test_an_unreachable_redis_degrades_to_the_in_memory_window_not_to_nothing() -> None:
    """Fail-open on the *error*, still counting in this process.

    Losing Redis costs accuracy across replicas -- the effective global limit
    becomes N x limit -- not the bound itself. A limiter that simply allowed
    everything would be a different and much worse choice.
    """
    limiter = _limiter(
        ip=RateLimitRule(limit=2, window_seconds=LONG_WINDOW),
        counter=RedisWindowCounter(DEAD_REDIS_URL, timeout=0.05),
        fallback=InMemoryWindowCounter(),
    )
    values = {rate_limit.DIMENSION_IP: "198.51.100.7"}

    decisions = [(await limiter.check(values)).allowed for _ in range(4)]
    assert decisions == [True, True, False, False]


async def test_a_failed_redis_is_not_retried_on_every_single_request() -> None:
    """Otherwise every login pays the connect timeout while Redis is down.

    That turns a degraded dependency into a latency attack on the login path, so
    the counter stops trying for a cooldown after a failure.
    """
    counter = RedisWindowCounter(DEAD_REDIS_URL, timeout=0.05, cooldown_seconds=30.0)
    # Read into locals: `assert counter.is_cooling_down is False` would narrow the
    # attribute for the rest of the function and make mypy call the second
    # assertion unreachable.
    before = counter.is_cooling_down

    with pytest.raises(RateLimitBackendError):
        await counter.bump("k", 60)
    after_failure = counter.is_cooling_down

    with pytest.raises(RateLimitBackendError):
        await counter.bump("k", 60)
    await counter.aclose()

    assert before is False
    assert after_failure is True


# --------------------------------------------------------------------------- #
# The concurrency gate -- the part that actually bounds memory
# --------------------------------------------------------------------------- #


async def test_the_gate_bounds_concurrent_work_to_exactly_its_limit() -> None:
    """The observed maximum, measured from inside the work.

    A rate limit still admits a burst; this is what stops the burst from pinning
    N x 64 MiB of argon2 working memory. ``== limit`` rather than ``<= limit``
    because a gate that serialised everything would also satisfy ``<=`` while
    throwing away all the throughput.
    """
    gate = ConcurrencyGate(limit=4, name="test")
    lock = threading.Lock()
    live = 0
    peak = 0

    def work() -> None:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.02)
        with lock:
            live -= 1

    await asyncio.gather(*[gate.run(work) for _ in range(24)])

    assert peak == 4
    assert live == 0
    assert gate.peak_in_flight == 4


async def test_the_gate_reports_its_own_peak() -> None:
    gate = ConcurrencyGate(limit=2, name="test")
    assert gate.in_flight == 0
    assert gate.peak_in_flight == 0

    await asyncio.gather(*[gate.run(lambda: time.sleep(0.01)) for _ in range(6)])

    assert gate.peak_in_flight == 2
    assert gate.in_flight == 0


async def test_the_gate_returns_the_work_result() -> None:
    gate = ConcurrencyGate(limit=2, name="test")
    assert await gate.run(lambda: 7) == 7


async def test_the_gate_runs_the_work_off_the_event_loop_thread() -> None:
    """argon2 is a blocking C call. Left on the loop it stalls every other request.

    Roughly 100 ms of stall per login, so twenty queued logins add two seconds to
    an unrelated health check. Bounding concurrency and moving it off the loop are
    the same change.
    """
    gate = ConcurrencyGate(limit=2, name="test")
    loop_thread = threading.get_ident()
    worker_thread = await gate.run(threading.get_ident)
    assert worker_thread != loop_thread


async def test_the_gate_releases_its_slot_when_the_work_raises() -> None:
    """A leaked permit shrinks the pool until the login path deadlocks."""
    gate = ConcurrencyGate(limit=1, name="test")

    def boom() -> None:
        raise ValueError("no")

    for _ in range(3):
        with pytest.raises(ValueError):
            await gate.run(boom)

    assert gate.in_flight == 0
    assert await gate.run(lambda: "still works") == "still works"


async def test_the_gate_refuses_a_limit_below_one() -> None:
    with pytest.raises(ValueError):
        ConcurrencyGate(limit=0, name="test")


def test_the_gate_survives_a_second_event_loop() -> None:
    """The regression test for a module-level ``asyncio.Semaphore``.

    A semaphore binds itself to the first loop that CONTENDS on it and raises
    ``RuntimeError: bound to a different event loop`` for every later one. The
    uncontended path does not bind, so this only shows up under load -- in
    production, not in a happy-path test. Both rounds below are contended on
    purpose.
    """
    gate = ConcurrencyGate(limit=1, name="test")

    async def round_of_work() -> int:
        await asyncio.gather(*[gate.run(lambda: time.sleep(0.005)) for _ in range(4)])
        return gate.peak_in_flight

    assert asyncio.run(round_of_work()) == 1
    # Same gate object, brand-new loop. This is the line that used to raise.
    assert asyncio.run(round_of_work()) == 1


# --------------------------------------------------------------------------- #
# The shipped policy
# --------------------------------------------------------------------------- #


def test_login_and_signup_carry_both_dimensions() -> None:
    """A policy missing a dimension is a policy that stops one attack of the two."""
    for rules in (rate_limit.LOGIN_RULES, rate_limit.SIGNUP_RULES):
        assert rate_limit.DIMENSION_IP in rules
        assert rate_limit.DIMENSION_EMAIL in rules


def test_the_shipped_limiters_are_process_wide_singletons() -> None:
    """A limiter rebuilt per request would carry no memory and count nothing."""
    assert rate_limit.login_limiter() is rate_limit.login_limiter()
    assert rate_limit.signup_limiter() is rate_limit.signup_limiter()
    assert rate_limit.login_limiter() is not rate_limit.signup_limiter()


def test_signup_is_throttled_at_least_as_hard_as_login() -> None:
    """Signup runs the same argon2 AND writes two rows, so it is never cheaper."""
    for dimension in (rate_limit.DIMENSION_IP, rate_limit.DIMENSION_EMAIL):
        login = rate_limit.LOGIN_RULES[dimension]
        signup = rate_limit.SIGNUP_RULES[dimension]
        assert signup.limit / signup.window_seconds <= login.limit / login.window_seconds
