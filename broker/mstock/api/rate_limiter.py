"""Process-wide pacing for mStock Type B ORDER calls (10-orders/second plan).

mStock documents its ORDER-API budget as **30/sec, 250/min and 1000/hour** per
API key (data APIs 1/sec and 1000/min; quote APIs 20/sec). This server runs on a
**10-orders-per-second plan**, so the gate is built at THAT ceiling rather than
the published maximum: pacing above what the subscription allows is exactly what
makes the backend answer

    {"status": "false", "message": "Invalid Input : ... exceeds Limit ..."}

and on an exit sweep a rejected close is a REAL failure
(`broker/mstock/api/order_api.py#close_all_positions` reports it as such rather
than a false "squared off"), so a burst that trips the plan is a correctness
problem, not a cosmetic one.

Why module-level state
----------------------
Same reason as ``broker/tradesmart/api/rate_limiter.py`` and
``broker/fyers/api/rate_limiter.py``: services build a fresh broker module call
per request (``services/place_order_service.py``,
``services/close_position_service.py``), so any counter kept on an instance is
discarded on every call and paces nothing against concurrent callers. The
budget is per API KEY, which is one shared bucket for the whole process.

Why a sliding window instead of a fixed interval
------------------------------------------------
A sweep squaring off ten positions should go out at once, not be spread over ten
seconds. ``SlidingWindowGate`` reserves the earliest slot that satisfies EVERY
window, so a burst up to the allowance passes immediately and only the call past
it waits. The slot is reserved under the lock and slept for OUTSIDE it - holding
the lock across ``time.sleep`` would serialise every waiter behind the sleeper
and turn a 10/sec allowance into a queue.

Overrides
---------
Each ceiling reads an environment variable at import, so a plan upgrade is a
restart rather than a code change::

    MSTOCK_ORDER_MAX_PER_SECOND=10   # your plan's order ceiling
    MSTOCK_ORDER_MAX_PER_MINUTE=250  # documented order ceiling
    MSTOCK_ORDER_MAX_PER_HOUR=1000   # documented order ceiling

The minute/hour defaults are mStock's DOCUMENTED figures. They can only ever
make this gate stricter, never looser, and they stop a long strategy run from
drifting past a limit the per-second window cannot see. Confirm them against
your own plan if it differs from the standard one.
"""

import os
import threading
import time
from collections import deque

from utils.logging import get_logger

logger = get_logger(__name__)

# The plan this server runs on.
DEFAULT_ORDER_MAX_PER_SECOND = 10

# mStock's documented order-API ceilings (upper bounds, plan permitting).
DEFAULT_ORDER_MAX_PER_MINUTE = 250
DEFAULT_ORDER_MAX_PER_HOUR = 1000

# A rate-limit rejection is a DEFINITE non-acceptance (the broker refused
# before accepting), so retrying is safe - unlike a timeout, where the order
# may already be live. Kept small: a backoff long enough to clear the window,
# short enough not to stall an exit.
MAX_RETRIES = 2
BASE_BACKOFF = 1.0  # seconds; 1s then 2s


class SlidingWindowGate:
    """Reserve the earliest instant that satisfies every rolling window.

    ``windows`` is a sequence of ``(seconds, limit)`` pairs. ``clock`` is
    injectable so tests can drive the gate without sleeping.
    """

    def __init__(self, windows, clock=time.time):
        # Keep only windows that actually constrain something.
        self._windows = tuple(
            (float(seconds), int(limit))
            for seconds, limit in windows
            if int(limit) > 0 and float(seconds) > 0
        )
        self._clock = clock
        self._lock = threading.Lock()
        self._slots = deque()
        self._longest_window = max(
            (seconds for seconds, _ in self._windows), default=0.0
        )

    def reserve(self) -> float:
        """Reserve a slot and return how many seconds the caller must wait."""
        with self._lock:
            now = self._clock()

            # Anything older than the longest window can no longer constrain a
            # future slot.
            cutoff = now - self._longest_window
            while self._slots and self._slots[0] <= cutoff:
                self._slots.popleft()

            slot = now
            for window, limit in self._windows:
                in_window = [entry for entry in self._slots if entry > now - window]
                if len(in_window) < limit:
                    continue
                # One call too many inside this window: the oldest entry that
                # has to age out first is the (len - limit)-th from the start.
                boundary = in_window[len(in_window) - limit]
                slot = max(slot, boundary + window)

            self._slots.append(slot)
            return max(0.0, slot - now)

    def reset(self):
        """Forget every reservation (tests, or a deliberate gate reset)."""
        with self._lock:
            self._slots.clear()


def _env_int(name: str, default: int) -> int:
    """Positive int from the environment, falling back to ``default``.

    A missing, blank, non-numeric or non-positive value keeps the default
    instead of disabling the gate: a typo in an env var must never silently
    remove pacing and let a burst hit the plan's ceiling.
    """
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(f"{name}={raw!r} is not an integer - using {default}")
        return default
    if value <= 0:
        logger.warning(f"{name}={value} is not positive - using {default}")
        return default
    return value


_order_gate = SlidingWindowGate(
    (
        (1.0, _env_int("MSTOCK_ORDER_MAX_PER_SECOND", DEFAULT_ORDER_MAX_PER_SECOND)),
        (60.0, _env_int("MSTOCK_ORDER_MAX_PER_MINUTE", DEFAULT_ORDER_MAX_PER_MINUTE)),
        (3600.0, _env_int("MSTOCK_ORDER_MAX_PER_HOUR", DEFAULT_ORDER_MAX_PER_HOUR)),
    )
)


def apply_order_rate_limit() -> float:
    """Block until it is safe to issue another mStock ORDER call.

    Returns the seconds waited, which is handy for logging and for tests.
    """
    sleep_time = _order_gate.reserve()
    if sleep_time > 0:
        time.sleep(sleep_time)
    return sleep_time


def is_rate_limit_error(response) -> bool:
    """Whether mStock is reporting a rate-limit rejection.

    mStock answers a breach with ``status: "false"`` and a message naming the
    limit, e.g. ``"Invalid Input :  Order Recieved 11 in a current second exceeds
    Limit 10 for user"``. Matched on the phrase rather than the numbers so a
    changed plan ceiling is still recognised.
    """
    if not isinstance(response, dict):
        return False
    message = str(response.get("message") or response.get("errorcode") or "")
    lowered = message.lower()
    return "exceeds limit" in lowered or "rate limit" in lowered


def retry_delay(attempt: int) -> float:
    """Backoff before retrying a rate-limited call: 1s, then 2s."""
    return BASE_BACKOFF * (2**attempt)
