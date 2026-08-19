"""Rate limiting and concurrency bounding for the unauthenticated credential routes.

``/auth/login`` and ``/auth/signup`` both run argon2id at 64 MiB x 4 lanes. That
parameter choice is right for cracking resistance and it is exactly what makes
those two routes the cheapest denial of service in the product: N concurrent
attempts pin N x 64 MiB of working memory, so an attacker with no credentials at
all can exhaust a replica far more cheaply than they can guess a password. A
twelve-character password minimum does nothing about that.

This module contains the two halves of the answer, and they are not
interchangeable.

**A fixed-window rate limiter, over two independent dimensions.** Per IP and per
email, counted separately, because they stop different attacks: credential
stuffing holds the IP and varies the email, while a targeted attack on one account
holds the email and varies the IP. One combined key would look correct and stop
neither.

**A concurrency gate around the hashing itself.** This is the half that actually
bounds memory, and the half that is usually left out. A rate limit is a bound on
requests *per window*; it still permits the whole window's budget to arrive in one
burst. With a limit of 30 in 5 minutes and no gate, 30 simultaneous logins are
inside policy and still 1.9 GiB of argon2. The gate caps how many hashes run at
once, so peak memory is ``PASSWORD_HASH_CONCURRENCY x 64 MiB`` no matter what the
arrival pattern is.

The gate also moves the hash off the event loop. argon2-cffi is a blocking C call,
so a login left on the loop stalls every other request in the process for the
duration -- roughly 100 ms each, which twenty queued logins turn into two seconds
of latency on an unrelated health check. Bounding it and offloading it are the same
change.

Fail-open or fail-closed on a Redis error?
------------------------------------------
**Fail OPEN on the error, and degrade to a process-local window rather than to
nothing.** A Redis outage must not become an authentication outage. Refusing every
login while the counter store is unreachable hands an attacker the exact denial of
service this module exists to prevent, by a route far cheaper than the one it
blocks: instead of exhausting memory they only have to make one Redis unreachable,
and every customer is locked out of the product. Redis restarts, failovers and
network blips are ordinary events; a total login outage on each one is not a
trade anyone would accept if it were written down as a requirement.

The argument for fail-closed is real and worth stating: a limiter that stops
counting is a limiter an attacker can switch off, so if the limiter were the only
thing standing between an attacker and something expensive or irreversible --
money moving, SMS being sent, an account being created at a vendor -- refusing
would be correct. It is not the case here, for two specific reasons:

* the property that matters, the **memory bound, does not depend on Redis at
  all**. :class:`ConcurrencyGate` is process-local. Losing Redis loses cross-replica
  *accounting*, not the ceiling that prevents the OOM.
* the fallback is not "no limit". It is the same fixed window kept in this
  process, so with N replicas the effective global budget becomes N x limit --
  looser, still bounded, and still fatal to a single-source flood.

So the choice costs precision under a dependency failure and buys availability,
while the safety property the work was commissioned for is untouched. A refusal
caused by a Redis error is also indistinguishable to the caller from a refusal
caused by their own behaviour, which trains users and support to treat 429 as
noise.

Two deployment notes that are part of the design, not footnotes:

* the per-IP dimension keys off ``request.client.host`` and this module never
  parses ``X-Forwarded-For``. Trusting that header on an internet-facing route
  lets an attacker put a fresh IP in every request and erase the dimension
  entirely. Behind a reverse proxy, run uvicorn with ``--proxy-headers`` and an
  explicit ``--forwarded-allow-ips`` so the *server* rewrites ``client`` from a
  trusted hop; without that every client collapses into one bucket, which
  over-limits rather than under-limits.
* counter keys are HMAC digests, never raw values, so a dump of the Redis
  keyspace is not an address book of the accounts currently under attack.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Protocol
from weakref import WeakKeyDictionary

import redis.asyncio as redis_asyncio
from redis.exceptions import RedisError

from backend.app.core.config import get_settings

#: The two dimensions. Names are keys in the rule and value mappings, and they
#: appear in counter keys, so changing one resets that window once.
DIMENSION_IP: Final = "ip"
DIMENSION_EMAIL: Final = "email"

#: How many argon2 hashes may run at once, process-wide. Peak hashing memory is
#: this times the 64 MiB in ``core.security``, so 4 lanes x 4 slots is ~256 MiB --
#: a number that fits a small container with room for everything else.
PASSWORD_HASH_CONCURRENCY: Final = 4

#: Soft ceiling on the in-memory fallback's key set. Every window is a distinct
#: key, so without a bound an unreachable Redis turns the fallback into a slow
#: memory leak driven by the attacker.
IN_MEMORY_MAX_KEYS: Final = 20_000

#: Redis is on the login critical path, so it gets a tight timeout: a hung Redis
#: must not become login latency.
DEFAULT_REDIS_TIMEOUT_SECONDS: Final = 0.25

#: After a failure, stop trying for this long. Without it every request pays the
#: connect timeout for as long as Redis is down, which converts a degraded
#: dependency into a latency attack on the login path.
DEFAULT_REDIS_COOLDOWN_SECONDS: Final = 10.0


class RateLimitBackendError(RuntimeError):
    """The shared counter store could not be reached, or did not answer in time."""


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    """``limit`` events permitted per ``window_seconds``, per key.

    A fixed window rather than a sliding one or a token bucket. It is the cheapest
    thing that is correct at the boundary (one INCR and one EXPIRE), and its known
    weakness -- up to ``2 x limit`` across a window edge -- is irrelevant against
    the threat here, because the burst is separately capped by
    :class:`ConcurrencyGate`. A sliding window would buy edge precision we have no
    use for at the cost of a sorted set per key.
    """

    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be at least 1: zero would refuse the first request")
        if self.window_seconds < 1:
            raise ValueError("window_seconds must be at least 1")


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """The answer for one request."""

    allowed: bool
    #: Seconds until the tripped window rolls over. Goes straight into
    #: ``Retry-After``; zero when nothing tripped.
    retry_after_seconds: int = 0
    #: Which dimension tripped, for logs and tests. **Never for the response
    #: body:** telling a caller which of the two limits it hit tells it which one
    #: to route around.
    dimension: str | None = None


# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #


class WindowCounter(Protocol):
    """Increment the count for ``key`` in its current window and return it."""

    async def bump(self, key: str, window_seconds: int) -> int: ...


@dataclass(slots=True)
class _Window:
    count: int
    expires_at: float


class InMemoryWindowCounter:
    """A process-local fixed window.

    Used as the fallback when Redis is unreachable, and as the whole counter on a
    developer machine or in a test. It needs no lock: :meth:`bump` contains no
    ``await``, so the event loop cannot interleave two of them.

    With more than one replica this counts per replica, which is exactly the
    accuracy loss the module docstring accepts in exchange for staying up.
    """

    def __init__(self) -> None:
        self._windows: dict[str, _Window] = {}

    @property
    def tracked_keys(self) -> int:
        """Live key count. Exists so a test can prove the pruning happens."""
        return len(self._windows)

    async def bump(self, key: str, window_seconds: int) -> int:
        now = time.monotonic()
        window = self._windows.get(key)
        if window is None or window.expires_at <= now:
            if len(self._windows) >= IN_MEMORY_MAX_KEYS:
                self._prune(now)
            window = _Window(count=0, expires_at=now + window_seconds)
            self._windows[key] = window
        window.count += 1
        return window.count

    def _prune(self, now: float) -> None:
        """Drop elapsed windows; if that frees nothing, drop everything.

        The second half is deliberate. Every key here is derived from
        attacker-supplied input, so a flood of distinct IPs can fill the map with
        entries that are all still live. Clearing then is a bounded loss --
        somebody gets a fresh budget -- and it is strictly better than growing
        without limit until the process dies, which is the failure this whole
        module is about.
        """
        self._windows = {k: w for k, w in self._windows.items() if w.expires_at > now}
        if len(self._windows) >= IN_MEMORY_MAX_KEYS:
            self._windows.clear()


class RedisWindowCounter:
    """A fixed window shared by every replica, in Redis.

    ``INCR`` then ``EXPIRE`` in one pipeline. INCR on a missing key creates it at
    1, so the first request in a window and the hundredth take the same path and
    there is no check-then-set race between two replicas.

    The client is created per event loop. A ``redis.asyncio`` client binds to the
    loop it was created on, and this object is a process-wide singleton, so a
    single cached client would break in any process that runs more than one loop.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = DEFAULT_REDIS_TIMEOUT_SECONDS,
        cooldown_seconds: float = DEFAULT_REDIS_COOLDOWN_SECONDS,
    ) -> None:
        self._url = url
        self._timeout = timeout
        self._cooldown = cooldown_seconds
        self._blocked_until = 0.0
        self._clients: WeakKeyDictionary[asyncio.AbstractEventLoop, redis_asyncio.Redis] = (
            WeakKeyDictionary()
        )

    @property
    def is_cooling_down(self) -> bool:
        """Whether the cooldown after a failure is still in effect."""
        return time.monotonic() < self._blocked_until

    def _client(self) -> redis_asyncio.Redis:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = redis_asyncio.Redis.from_url(
                self._url,
                socket_timeout=self._timeout,
                socket_connect_timeout=self._timeout,
            )
            self._clients[loop] = client
        return client

    async def bump(self, key: str, window_seconds: int) -> int:
        if self.is_cooling_down:
            raise RateLimitBackendError("redis is in its post-failure cooldown")
        try:
            client = self._client()
            async with client.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, window_seconds)
                results = await pipe.execute()
            return int(results[0])
        except (TimeoutError, RedisError, OSError, ValueError) as exc:
            self._blocked_until = time.monotonic() + self._cooldown
            raise RateLimitBackendError(f"{type(exc).__name__}: {exc}") from exc

    async def aclose(self) -> None:
        """Close this loop's client. For tests and for an orderly shutdown."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - only outside a loop
            return
        client = self._clients.pop(loop, None)
        if client is not None:
            await client.aclose()


# --------------------------------------------------------------------------- #
# The limiter
# --------------------------------------------------------------------------- #


class FixedWindowRateLimiter:
    """Check a request against every dimension it carries.

    All applicable dimensions are counted, not just up to the first that trips: a
    request that costs an argon2 verification has cost it whichever window
    noticed, and skipping the second count would let an attacker keep one
    dimension permanently empty by ensuring the other always trips first.
    """

    def __init__(
        self,
        *,
        rules: Mapping[str, RateLimitRule],
        counter: WindowCounter,
        fallback: WindowCounter | None = None,
        namespace: str,
        secret: str,
    ) -> None:
        self._rules = dict(rules)
        self._counter = counter
        self._fallback = fallback
        self._namespace = namespace
        self._secret = secret

    @property
    def rules(self) -> Mapping[str, RateLimitRule]:
        return self._rules

    @property
    def namespace(self) -> str:
        return self._namespace

    def _key(self, dimension: str, value: str, bucket: int) -> str:
        """A keyed digest of the dimension value, never the value itself.

        HMAC rather than a bare SHA-256: an email address has far too little
        entropy to resist a dictionary attack on a plain digest, so a leaked
        keyspace would still name the accounts under attack. Domain-separated
        from session signing by the ``rl.v1`` prefix, so the two uses of the
        secret cannot be made to collide.
        """
        message = f"rl.v1:{dimension}:{value}".encode()
        digest = hmac.new(self._secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
        return f"{self._namespace}:{dimension}:{digest[:32]}:{bucket}"

    @staticmethod
    def _normalise(dimension: str, value: str) -> str:
        if dimension == DIMENSION_EMAIL:
            # The same normalisation the account itself uses, or every alternative
            # spelling of one address would be a fresh budget. Deliberately not
            # `auth_service.normalise_email`: core must not import a service, and
            # a value this rejects still needs counting.
            return value.strip().lower()
        return value.strip()

    async def _count(self, key: str, window_seconds: int) -> int | None:
        """The count, or ``None`` when no counter could answer.

        ``None`` is the fail-open path, and it is the only place that decision is
        made. See the module docstring for why it is open rather than closed.
        """
        try:
            return await self._counter.bump(key, window_seconds)
        except RateLimitBackendError:
            pass

        fallback = self._fallback
        if fallback is None:
            return None
        try:
            return await fallback.bump(key, window_seconds)
        except RateLimitBackendError:  # pragma: no cover - the fallback is local
            return None

    async def check(self, values: Mapping[str, str]) -> RateLimitDecision:
        """Count this request and say whether it may proceed."""
        now = time.time()
        worst: RateLimitDecision | None = None

        for dimension, raw in values.items():
            rule = self._rules.get(dimension)
            if rule is None:
                continue

            bucket = int(now) // rule.window_seconds
            key = self._key(dimension, self._normalise(dimension, raw), bucket)
            count = await self._count(key, rule.window_seconds)
            if count is None or count <= rule.limit:
                continue

            retry_after = rule.window_seconds - (int(now) % rule.window_seconds)
            if worst is None or retry_after > worst.retry_after_seconds:
                worst = RateLimitDecision(
                    allowed=False, retry_after_seconds=max(1, retry_after), dimension=dimension
                )

        return worst if worst is not None else RateLimitDecision(allowed=True)


# --------------------------------------------------------------------------- #
# The concurrency gate
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _GateState:
    semaphore: asyncio.Semaphore
    in_flight: int = 0
    peak_in_flight: int = 0


class ConcurrencyGate:
    """Bound how many blocking calls run at once, and run them off the loop.

    One semaphore **per event loop**, not one per gate. ``asyncio.Semaphore``
    binds itself to the first loop that contends on it and raises
    ``RuntimeError: ... bound to a different event loop`` for every other one --
    and the uncontended fast path does not bind, so a module-level semaphore
    looks fine in every happy-path test and fails only under the load it exists
    to handle. Keyed weakly on the loop so a finished loop's state is collected.

    The counters are per loop for the same reason, which also keeps
    :attr:`peak_in_flight` meaningful in a test rather than a running total across
    every test in the session.
    """

    def __init__(self, limit: int, *, name: str) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self._limit = limit
        self._name = name
        self._states: WeakKeyDictionary[asyncio.AbstractEventLoop, _GateState] = WeakKeyDictionary()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def name(self) -> str:
        return self._name

    def _state(self) -> _GateState:
        loop = asyncio.get_running_loop()
        state = self._states.get(loop)
        if state is None:
            state = _GateState(semaphore=asyncio.Semaphore(self._limit))
            self._states[loop] = state
        return state

    @property
    def in_flight(self) -> int:
        """How many calls hold a slot right now, on this loop."""
        return self._state().in_flight

    @property
    def peak_in_flight(self) -> int:
        """The high-water mark on this loop. The number a test should assert."""
        return self._state().peak_in_flight

    async def run[T](self, work: Callable[[], T]) -> T:
        """Run a blocking callable in a worker thread, at most ``limit`` at a time.

        The slot is taken *before* the thread is handed the work and released in a
        ``finally``, so an exception in ``work`` cannot leak a permit -- a leaked
        permit shrinks the pool permanently and eventually deadlocks the login
        path, which would be a worse outage than the one being prevented.
        """
        state = self._state()
        async with state.semaphore:
            state.in_flight += 1
            state.peak_in_flight = max(state.peak_in_flight, state.in_flight)
            try:
                return await asyncio.to_thread(work)
            finally:
                state.in_flight -= 1


# --------------------------------------------------------------------------- #
# The shipped policy
# --------------------------------------------------------------------------- #

#: Login. The IP window is loose enough for an office behind one NAT address and
#: still cheap to serve; the email window is tighter because a single account
#: seeing ten attempts in a quarter of an hour is not a person who forgot.
LOGIN_RULES: Final[Mapping[str, RateLimitRule]] = {
    DIMENSION_IP: RateLimitRule(limit=30, window_seconds=300),
    DIMENSION_EMAIL: RateLimitRule(limit=10, window_seconds=900),
}

#: Signup. Never cheaper per event than login: it runs the same argon2 and also
#: writes two rows, and nobody legitimately creates twelve accounts an hour.
SIGNUP_RULES: Final[Mapping[str, RateLimitRule]] = {
    DIMENSION_IP: RateLimitRule(limit=12, window_seconds=3600),
    DIMENSION_EMAIL: RateLimitRule(limit=4, window_seconds=3600),
}


@lru_cache(maxsize=1)
def _shared_redis_counter() -> RedisWindowCounter:
    return RedisWindowCounter(get_settings().redis_url)


def _limiter(rules: Mapping[str, RateLimitRule], namespace: str) -> FixedWindowRateLimiter:
    return FixedWindowRateLimiter(
        rules=rules,
        counter=_shared_redis_counter(),
        fallback=InMemoryWindowCounter(),
        namespace=f"sma:rl:{namespace}",
        secret=get_settings().session_secret,
    )


@lru_cache(maxsize=1)
def login_limiter() -> FixedWindowRateLimiter:
    """The process-wide login limiter. Cached: a fresh one would count nothing."""
    return _limiter(LOGIN_RULES, "login")


@lru_cache(maxsize=1)
def signup_limiter() -> FixedWindowRateLimiter:
    """The process-wide signup limiter, on its own namespace and its own policy."""
    return _limiter(SIGNUP_RULES, "signup")
