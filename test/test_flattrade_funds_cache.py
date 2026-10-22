"""Tests for the Flattrade funds path (cache, concurrency, failure handling).

Hermetic by design: the broker transport is replaced with a fake, so nothing
here touches Flattrade or spends the account's rate-limit budget. What is being
verified is the behaviour that used to make `/api/v1/funds` slow --
two serialized broker calls per request, repeated several times a second with
no cache.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

# Import the module under test from the repo root regardless of pytest's rootdir.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from broker.flattrade.api import funds  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_funds_state(monkeypatch):
    """Fresh cache + flights, a stable (short-circuited) TTL, and an account id."""
    monkeypatch.setenv("BROKER_API_KEY", "FZ31551:::testkey")
    monkeypatch.setattr(funds, "_FUNDS_CACHE_TTL", 30.0, raising=False)
    funds.clear_funds_cache()
    funds._flights.clear()
    yield
    funds.clear_funds_cache()
    funds._flights.clear()


def _limits_ok(cash="100000.00", payin="0", marginused="25000.00", collateral="5000.00"):
    return {
        "stat": "Ok",
        "cash": cash,
        "payin": payin,
        "marginused": marginused,
        "brkcollamt": collateral,
    }


def _positions(*entries):
    return list(entries)


POS_LONG = {"urmtom": "1500.50", "rpnl": "250.25", "netqty": "50"}


def _fake_fetch(calls, limits=None, positions=None, delay=0.0, lock=None):
    """Return a fetch_data replacement that records what was requested."""

    def _fetch(endpoint, payload, headers, client):
        if delay:
            time.sleep(delay)
        if lock is not None:
            with lock:
                calls.append(endpoint)
        else:
            calls.append(endpoint)
        if endpoint.endswith("/Limits"):
            return _limits_ok() if limits is None else limits
        return _positions(POS_LONG) if positions is None else positions

    return _fetch


# --- the two bugs this fix exists for -------------------------------------


def test_second_call_within_ttl_hits_the_cache_not_the_broker(monkeypatch):
    """The dashboard polls several times a second; only the first pays."""
    calls = []
    monkeypatch.setattr(funds, "fetch_data", _fake_fetch(calls))

    first = funds.get_margin_data("token-a")
    second = funds.get_margin_data("token-a")
    third = funds.get_margin_data("token-a")

    assert first == second == third
    assert first["availablecash"] == "75000.00"  # 100000 + 0 - 25000
    # One pair of broker calls for three requests, not three pairs.
    assert sorted(calls) == ["/PiConnectAPI/Limits", "/PiConnectAPI/PositionBook"]


def test_limits_and_positionbook_are_issued_concurrently(monkeypatch):
    """Serialized round-trips was the other half of the slowness."""
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def _fetch(endpoint, payload, headers, client):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with state_lock:
            active -= 1
        if endpoint.endswith("/Limits"):
            return _limits_ok()
        return _positions(POS_LONG)

    monkeypatch.setattr(funds, "fetch_data", _fetch)

    started = time.monotonic()
    result = funds.get_margin_data("token-b")
    elapsed = time.monotonic() - started

    assert result["m2munrealized"] == "1500.50"
    assert peak == 2, "Limits and PositionBook must overlap, not queue"
    # Two 150ms calls that overlap finish in ~150ms; serialized would be ~300ms.
    assert elapsed < 0.30, f"requests were serialized: {elapsed:.3f}s"


def test_concurrent_misses_collapse_into_one_broker_fetch(monkeypatch):
    """Single-flight: N simultaneous pollers must not become N pairs of calls."""
    calls = []
    lock = threading.Lock()
    monkeypatch.setattr(funds, "fetch_data", _fake_fetch(calls, delay=0.2, lock=lock))

    results = []
    results_lock = threading.Lock()

    def _call():
        value = funds.get_margin_data("token-c")
        with results_lock:
            results.append(value)

    threads = [threading.Thread(target=_call) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 6
    assert all(r == results[0] for r in results)
    # 6 callers, one fetch pair.
    assert sorted(calls) == ["/PiConnectAPI/Limits", "/PiConnectAPI/PositionBook"]


# --- failure handling: must never raise, must never be mistaken for success -


class _ExplodingClient:
    def post(self, *args, **kwargs):
        raise OSError("[WinError 10035] A non-blocking socket operation could not be completed immediately")


class _HtmlClient:
    """An edge proxy answering with an HTML error page instead of JSON."""

    class _Response:
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    def post(self, *args, **kwargs):
        return self._Response()


@pytest.mark.parametrize("client", [_ExplodingClient(), _HtmlClient()])
def test_transport_failure_returns_empty_dict_and_never_raises(monkeypatch, client):
    monkeypatch.setattr(funds, "get_httpx_client", lambda: client)

    assert funds.get_margin_data("token-d") == {}


def test_a_failure_is_not_cached(monkeypatch):
    """An empty result must stay retryable, not pin blank PnL for the TTL."""
    calls = []
    monkeypatch.setattr(funds, "get_httpx_client", lambda: _ExplodingClient())

    assert funds.get_margin_data("token-e") == {}

    # Now the broker recovers; the very next call must go and ask again.
    monkeypatch.setattr(funds, "fetch_data", _fake_fetch(calls))
    assert funds.get_margin_data("token-e")["m2munrealized"] == "1500.50"
    assert sorted(calls) == ["/PiConnectAPI/Limits", "/PiConnectAPI/PositionBook"]


def test_broker_error_status_returns_empty_dict(monkeypatch):
    calls = []
    monkeypatch.setattr(
        funds, "fetch_data", _fake_fetch(calls, limits={"stat": "Not_Ok", "emsg": "Invalid Session"})
    )

    assert funds.get_margin_data("token-f") == {}


def test_non_numeric_broker_field_returns_empty_dict(monkeypatch):
    """A null field in the broker's reply must not escape as a TypeError."""
    monkeypatch.setattr(funds, "fetch_data", _fake_fetch([], limits=_limits_ok(cash=None)))

    assert funds.get_margin_data("token-g") == {}


# --- correctness of the figure itself, and cache-key hygiene ----------------


def test_funds_are_computed_from_both_endpoints(monkeypatch):
    positions = _positions(
        {"urmtom": "1500.50", "rpnl": "250.25", "netqty": "50"},
        {"urmtom": "-400.25", "rpnl": "100.00", "netqty": "-10"},
    )
    monkeypatch.setattr(funds, "fetch_data", _fake_fetch([], positions=positions))

    result = funds.get_margin_data("token-h")

    assert result["m2munrealized"] == "1100.25"  # 1500.50 - 400.25
    assert result["m2mrealized"] == "350.25"  # 250.25 + 100.00
    assert result["collateral"] == "5000.00"
    assert result["utiliseddebits"] == "25000.00"


def test_different_tokens_do_not_share_a_cache_entry(monkeypatch):
    calls = []
    monkeypatch.setattr(funds, "fetch_data", _fake_fetch(calls))

    funds.get_margin_data("token-i")
    funds.get_margin_data("token-j")

    assert len(calls) == 4  # two separate pairs, not one shared snapshot


def test_cache_key_never_contains_the_raw_token():
    key = funds._account_key("super-secret-session-token")

    assert "super-secret-session-token" not in key
    assert len(key) == 12


def test_dead_fetch_data_signature_is_unchanged(monkeypatch):
    """`fetch_data(endpoint, payload, headers, client)` is called positionally."""
    seen = {}

    class _Client:
        def post(self, url, content=None, headers=None, timeout=None):
            seen["url"] = url
            seen["timeout"] = timeout

            class _R:
                def json(self):
                    return {"stat": "Ok"}

            return _R()

    result = funds.fetch_data("/PiConnectAPI/Limits", "jData={}&jKey=x", {}, _Client())

    assert result == {"stat": "Ok"}
    assert seen["url"] == "https://piconnect.flattrade.in/PiConnectAPI/Limits"
    assert seen["timeout"] == funds._FUNDS_TIMEOUT
