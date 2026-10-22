# api/funds.py
"""Flattrade funds / margin.

`/api/v1/funds` is the path the dashboard's PnL display rides on, and it is
polled several times a second. Every one of those requests used to issue TWO
blocking broker round-trips -- `/PiConnectAPI/Limits` and then
`/PiConnectAPI/PositionBook` -- one after the other, with no cache anywhere on
the path, so the same two responses were being re-fetched 3-4 times a second.
A balance is not tick data; it cannot change that fast.

Four things were fixed here rather than at the caller:

1. The two endpoints are issued **concurrently**, so a request costs one
   round-trip instead of two serialized ones.
2. The processed result is served from a short-TTL, process-wide cache, and
   concurrent misses collapse into a **single** broker fetch (single-flight).
   Broker load drops by roughly 4x while the response gets faster.
3. Both calls now take a slot in Flattrade's account-wide rate limiter
   (`broker.flattrade.api.data`), which this path previously bypassed
   entirely -- the busiest caller in the process was the only one not counted
   against the account's quota (10 orders/sec, 40 data req/sec, 200/min).
4. Each call carries a short **per-request timeout** instead of inheriting the
   shared client's 120s, which is sized for large historical downloads.

Only a usable response is cached: an empty result stays retryable, so one
broker hiccup cannot pin "no funds" for the whole TTL. A transport failure or
a non-JSON body still returns ``{}`` -- the contract callers already depend on
(`blueprints/auth.py` reads an empty result as an invalid token, so a raised
exception here would surface as a forced broker re-login).
"""

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from cachetools import TTLCache

from broker.flattrade.api.data import _apply_rate_limit
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

FLATTRADE_API_BASE = "https://piconnect.flattrade.in"

# A funds snapshot is not tick data: the dashboard polls faster than a balance
# can change. One second collapses a burst of polls into a single broker fetch
# and still reads as live to a human. Set FLATTRADE_FUNDS_CACHE_TTL=0 to
# disable the cache entirely.
_FUNDS_CACHE_TTL = float(os.getenv("FLATTRADE_FUNDS_CACHE_TTL", "1"))
_FUNDS_CACHE_MAXSIZE = int(os.getenv("FLATTRADE_FUNDS_CACHE_MAXSIZE", "8"))

# The shared client's timeout is 120s, sized for multi-year history downloads.
# A funds snapshot that has not answered in a few seconds is no longer useful
# to a PnL display, and waiting on it keeps a request thread busy.
_FUNDS_TIMEOUT = float(os.getenv("FLATTRADE_FUNDS_TIMEOUT", "5"))

_funds_cache: TTLCache = TTLCache(
    maxsize=max(_FUNDS_CACHE_MAXSIZE, 1), ttl=max(_FUNDS_CACHE_TTL, 0.001)
)
_funds_cache_lock = threading.Lock()


class _Flight:
    """One in-progress funds fetch, whose outcome is published to waiters."""

    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict | None = None
        self.error: BaseException | None = None


# Concurrent misses for the same account wait on the leader's fetch instead of
# each issuing its own pair of broker calls. On a rate-limited, per-account
# quota that difference matters: without it, three simultaneous pollers become
# six broker requests where two would do.
_flights: dict[str, _Flight] = {}
_flights_lock = threading.Lock()


def calculate_pnl(entry):
    """Calculate realized and unrealized PnL for a given entry."""
    # Use broker-provided values directly for more accurate calculation
    unrealized_pnl = float(entry.get("urmtom", 0))
    realized_pnl = float(entry.get("rpnl", 0))

    # Fallback calculation if broker values aren't available
    if unrealized_pnl == 0 and float(entry.get("netqty", 0)) != 0:
        price_factor = float(entry.get("prcftr", 1))
        unrealized_pnl = (
            (float(entry.get("lp", 0)) - float(entry.get("netavgprc", 0)))
            * float(entry.get("netqty", 0))
            * price_factor
        )

    return realized_pnl, unrealized_pnl


def fetch_data(endpoint, payload, headers, client):
    """POST one PiConnect endpoint and return the parsed JSON, or ``None``.

    Never raises, and never returns a non-JSON object. Flattrade drops pooled
    connections under load (`httpx.ReadError: [WinError 10035]` shows up on
    this path and on quotes in `log/`), and an edge proxy can answer with an
    HTML error page. Either must degrade to "no funds available here" instead
    of escaping as a 500 -- and must never be mistaken for an expired token by
    `blueprints/auth.py`, which reads an empty funds result as invalid
    credentials and would force the user through a re-login.
    """
    # This path used to bypass Flattrade's account-wide limiter entirely,
    # making the busiest caller in the process the only one not counted
    # against the broker's quota. Reserve a slot before every request.
    _apply_rate_limit()
    try:
        response = client.post(
            f"{FLATTRADE_API_BASE}{endpoint}",
            content=payload,
            headers=headers,
            timeout=_FUNDS_TIMEOUT,
        )
        return response.json()
    except Exception as exc:  # noqa: BLE001 - a funds display must never 500
        # httpx.HTTPError covers timeouts/read errors/connection loss;
        # ValueError covers a non-JSON body.
        logger.warning(
            f"Flattrade funds call to {endpoint} failed: {type(exc).__name__}: {exc}"
        )
        return None


def _fetch_funds(auth_token):
    """Issue both broker calls concurrently and process them into funds data.

    Returns ``{}`` on any failure, which is the documented contract for this
    module (`get_margin_data` returns an empty dict rather than raising).
    """
    full_api_key = os.getenv("BROKER_API_KEY") or ""
    userid = full_api_key.split(":::")[0]
    actid = userid

    # Prepare payload
    data = {"uid": userid, "actid": actid}
    payload = f"jData={json.dumps(data)}&jKey={auth_token}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    # Get the shared httpx client
    client = get_httpx_client()

    # Concurrently, not one after the other. Ordering never mattered to
    # correctness -- the two responses are independent -- but issuing them
    # together halves the wall-clock cost of a cache miss, because the second
    # round-trip no longer waits for the first to return.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ft-funds") as pool:
        limits_future = pool.submit(fetch_data, "/PiConnectAPI/Limits", payload, headers, client)
        positions_future = pool.submit(
            fetch_data, "/PiConnectAPI/PositionBook", payload, headers, client
        )
        margin_data = limits_future.result()
        position_data = positions_future.result()

    if not isinstance(margin_data, dict) or margin_data.get("stat") != "Ok":
        emsg = margin_data.get("emsg") if isinstance(margin_data, dict) else "no response"
        logger.info(f"Error fetching margin data: {emsg}")
        return {}

    total_realised = 0
    total_unrealised = 0

    # Process position data if it's a list
    if isinstance(position_data, list):
        for entry in position_data:
            realized_pnl, unrealized_pnl = calculate_pnl(entry)
            total_realised += realized_pnl
            total_unrealised += unrealized_pnl

    try:
        # Calculate total_available_margin as the sum of 'cash' and 'payin'
        total_available_margin = (
            float(margin_data.get("cash", 0))
            + float(margin_data.get("payin", 0))
            - float(margin_data.get("marginused", 0))
        )
        total_collateral = float(margin_data.get("brkcollamt", 0))
        total_used_margin = float(margin_data.get("marginused", 0))

        # Construct and return the processed margin data
        return {
            "availablecash": f"{total_available_margin:.2f}",
            "collateral": f"{total_collateral:.2f}",
            "m2munrealized": f"{total_unrealised:.2f}",
            "m2mrealized": f"{total_realised:.2f}",
            "utiliseddebits": f"{total_used_margin:.2f}",
        }
    except (KeyError, TypeError, ValueError) as e:
        # Log the exception and return an empty dictionary if there's an
        # unexpected error. TypeError/ValueError join KeyError here because a
        # broker response with a null or non-numeric field would otherwise
        # raise straight through to the Flask request thread.
        logger.error(f"Error processing margin data: {e}")
        return {}


def _account_key(auth_token) -> str:
    """Cache key for the account behind this token.

    Hashed, never the raw token: a cache key can end up in a debug log line
    and the token is a live trading credential. Flattrade re-issues the token
    on every login, so the cache necessarily starts cold after a fresh
    session -- which is the behaviour we want, since a new login must never be
    served the previous session's numbers.
    """
    return hashlib.sha256(str(auth_token).encode()).hexdigest()[:12]


def get_margin_data(auth_token):
    """Fetch and process margin and position data.

    Served from a short-TTL cache when one is warm; callers that miss together
    share a single broker fetch. Returns ``{}`` when the broker cannot be
    reached or answers with an error, exactly as before.

    Note that the TTL bounds how long a *successful* answer may be replayed.
    It is deliberately short (1s default) so that a token revoked moments ago
    still fails validation on essentially the next call, and so that a
    transport failure is never masked by a stale success.
    """
    if _FUNDS_CACHE_TTL <= 0:
        return _fetch_funds(auth_token)

    key = _account_key(auth_token)

    with _funds_cache_lock:
        cached = _funds_cache.get(key)
    if cached is not None:
        logger.debug("Flattrade funds cache hit")
        return cached

    with _flights_lock:
        flight = _flights.get(key)
        leader = flight is None
        if leader:
            flight = _Flight()
            _flights[key] = flight

    if not leader:
        # Share the leader's outcome rather than adding a second identical
        # pair of calls to a rate-limited account. Waiting on a lock and
        # retrying would fan out on failure instead.
        if not flight.event.wait(timeout=_FUNDS_TIMEOUT * 2):
            logger.warning("Funds single-flight wait timed out; fetching directly")
            return _fetch_funds(auth_token)
        if flight.error is not None:
            raise flight.error
        return flight.result

    result: dict = {}
    try:
        result = _fetch_funds(auth_token)
        # Only a usable response is cached. An empty result is a failure --
        # caching it would pin "no funds" for the whole TTL and turn a single
        # broker hiccup into a second of blank PnL.
        if result:
            with _funds_cache_lock:
                _funds_cache[key] = result
        return result
    except BaseException as exc:  # noqa: BLE001 - propagated to waiters verbatim
        flight.error = exc
        raise
    finally:
        # Release waiters first, then retire the flight so the next caller
        # starts a fresh one.
        flight.result = result
        flight.event.set()
        with _flights_lock:
            _flights.pop(key, None)


def clear_funds_cache() -> None:
    """Drop the cached funds snapshot (test / administrative helper)."""
    with _funds_cache_lock:
        _funds_cache.clear()
