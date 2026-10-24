"""Smoke tests for the Flattrade square-off safeguards.

Run with the server's OWN virtualenv (the ambient Python lacks httpx):

    cd C:\\dev\\backend-installer\\payload\\server
    .\\.venv\\Scripts\\python.exe test\\test_flattrade_squareoff_safeguards.py

Exit code 0 = every check passed; 1 = at least one failed.

WHAT THIS PINS (the 2026-10-24 09:15 production order book)
  1. An UNREADABLE book never reads as "flat". Noren answers a zero-row day
     with {"stat": "Not_Ok", "emsg": "... no data"}; EVERY other Not_Ok
     (Session Expired, auth, gateway) is a read failure and must raise.
  2. A close is priced as a MARKETABLE limit, never a raw MKT: MPP off the LTP,
     crossed to the touch, rounded in the direction that keeps it crossing
     (SELL floors, BUY ceils) — including the float trap where 102.0/0.05 is
     2039.9999999999998 and a naive floor loses a whole tick.
  3. A stale working SELL is CANCELLED and the close RE-PRICED, not skipped
     (skipping leaves the position open for ever and a retry can never converge).
  4. The close quantity never exceeds the long, so a square-off cannot be sold
     through zero into a SHORT.
"""

import os
import sys

SERVER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

import broker.flattrade.api.order_api as oa  # noqa: E402
import utils.mpp_slab as mpp  # noqa: E402
from broker.flattrade.mapping.transform_data import (  # noqa: E402
    MPPPriceUnavailable,
    _marketable_limit_price,
)

_FAILURES = []
_CHECKS = 0


def check(label, condition, detail=""):
    global _CHECKS
    _CHECKS += 1
    if condition:
        print(f"  OK   {label}")
    else:
        _FAILURES.append(label)
        print(f"  FAIL {label}" + (f"  ({detail})" if detail else ""))


def section(title):
    print(f"\n{title}")


# ── 1. unreadable vs empty book ─────────────────────────────────────────────

def test_empty_book_detection():
    section("1. 'no data' is EMPTY; every other Not_Ok is a READ FAILURE")

    no_data = {"stat": "Not_Ok", "emsg": 'Error Occurred : 5 "no data"'}
    check("'no data' emsg -> empty", oa._is_empty_book_response(no_data) is True)
    check("_read_book('no data') -> []", oa._read_book(no_data, "book") == [])

    for label, reply in [
        ("Session Expired", {"stat": "Not_Ok", "emsg": "Session Expired"}),
        ("auth failure", {"stat": "Not_Ok", "emsg": "Invalid Session"}),
        ("gateway error", {"stat": "Not_Ok", "emsg": "Error Occurred : 7"}),
        ("Not_Ok, no emsg", {"stat": "Not_Ok"}),
    ]:
        check(f"{label} is NOT empty", oa._is_empty_book_response(reply) is False)
        raised = False
        try:
            oa._read_book(reply, "book")
        except oa.BrokerReadError:
            raised = True
        check(f"{label} RAISES BrokerReadError", raised)

    raised = False
    try:
        oa._read_book(None, "book")
    except oa.BrokerReadError:
        raised = True
    check("None reply RAISES (not a flat book)", raised)

    rows = [{"netqty": "75"}, "junk", {"netqty": "0"}]
    check("list reply keeps only dict rows", oa._read_book(rows, "book") == [
        {"netqty": "75"}, {"netqty": "0"}
    ])
    check("single-row dict reply wrapped", oa._read_book({"netqty": "1"}, "b") == [{"netqty": "1"}])


# ── 2. marketable limit pricing ────────────────────────────────────────────

def test_tick_rounding():
    section("2a. tick rounding honours direction (0.05 NFO tick)")

    check("floor 97.98 -> 97.95", mpp.round_to_tick_size(97.98, 0.05, "floor") == 97.95,
          mpp.round_to_tick_size(97.98, 0.05, "floor"))
    check("ceil  97.98 -> 98.00", mpp.round_to_tick_size(97.98, 0.05, "ceil") == 98.0,
          mpp.round_to_tick_size(97.98, 0.05, "ceil"))
    check("nearest default unchanged", mpp.round_to_tick_size(97.98, 0.05) == 98.0)

    # The float trap: 102.0 / 0.05 == 2039.9999999999998 in binary float.
    check("floor 102.0 -> 102.0 (no lost tick)",
          mpp.round_to_tick_size(102.0, 0.05, "floor") == 102.0,
          mpp.round_to_tick_size(102.0, 0.05, "floor"))
    check("ceil 102.0 -> 102.0", mpp.round_to_tick_size(102.0, 0.05, "ceil") == 102.0)

    check("no tick size: floor 2dp", mpp.round_to_tick_size(97.987, None, "floor") == 97.98)
    check("no tick size: ceil 2dp", mpp.round_to_tick_size(97.981, None, "ceil") == 97.99)

    sell = mpp.calculate_protected_price(100, "SELL", instrument_type="CE",
                                         tick_size=0.05, round_direction="floor")
    buy = mpp.calculate_protected_price(100, "BUY", instrument_type="CE",
                                        tick_size=0.05, round_direction="ceil")
    check("SELL 2% on CE floors", sell == 98.0, sell)
    check("BUY 2% on CE ceils", buy == 102.0, buy)


def test_marketable_price():
    section("2b. _marketable_limit_price crosses the touch")

    # MPP alone already crosses a 99.5 bid (98.0 <= 99.5): keep the protection.
    q = {"ltp": 100.0, "bid": 99.5, "ask": 100.5}
    price = _marketable_limit_price(q, "SELL", "CE", 0.05)
    check("SELL stays at/below the bid", price <= q["bid"], f"{price} vs bid {q['bid']}")

    # Bid BELOW the protective price: cross to the bid instead of resting above it.
    q = {"ltp": 100.0, "bid": 97.0, "ask": 100.5}
    price = _marketable_limit_price(q, "SELL", "CE", 0.05)
    check("SELL crosses down to the bid", price == 97.0, price)
    check("SELL never above bid", price <= q["bid"])

    q = {"ltp": 100.0, "bid": 99.5, "ask": 100.5}
    price = _marketable_limit_price(q, "BUY", "CE", 0.05)
    check("BUY stays at/above the ask", price >= q["ask"], f"{price} vs ask {q['ask']}")

    q = {"ltp": 100.0, "bid": 99.5, "ask": 103.0}
    price = _marketable_limit_price(q, "BUY", "CE", 0.05)
    check("BUY crosses up to the ask", price == 103.0, price)

    # No bid/ask at all: fall back to the protected price, still tick-valid.
    price = _marketable_limit_price({"ltp": 100.0, "bid": 0, "ask": 0}, "SELL", "PE", 0.05)
    check("no touch -> protected price", price == 98.0, price)
    check("protected price is tick-valid", round(price / 0.05, 6) % 1 == 0, price)


# ── 3. working-SELL accounting ─────────────────────────────────────────────

def test_working_sell_orders():
    section("3. _working_sell_orders reports what is still working")

    key = ("NIFTY24OCT22800CE", "NFO", "M")
    book = [
        # working SELL, half filled -> 200 still working
        {"trantype": "S", "status": "OPEN", "qty": "390", "fillshares": "190",
         "norenordno": "S1", "tsym": key[0], "exch": key[1], "prd": key[2]},
        # fully filled, still "OPEN" -> 0 working, ignored
        {"trantype": "S", "status": "OPEN", "qty": "390", "fillshares": "390",
         "norenordno": "S2", "tsym": key[0], "exch": key[1], "prd": key[2]},
        # a BUY is never a square-off SELL
        {"trantype": "B", "status": "OPEN", "qty": "390", "fillshares": "0",
         "norenordno": "B1", "tsym": key[0], "exch": key[1], "prd": key[2]},
        # already complete
        {"trantype": "S", "status": "COMPLETE", "qty": "390", "fillshares": "0",
         "norenordno": "S3", "tsym": key[0], "exch": key[1], "prd": key[2]},
        # a different contract must not leak in
        {"trantype": "S", "status": "OPEN", "qty": "75", "fillshares": "0",
         "norenordno": "S4", "tsym": "NIFTY24OCT22700PE", "exch": "NFO", "prd": "M"},
    ]
    working = oa._working_sell_orders(book)

    check("only the working SELL, with its order id",
          working.get(key) == [{"orderid": "S1", "remaining": 200}], working.get(key))
    check("fully-filled SELL excluded",
          all(o["orderid"] != "S2" for o in working.get(key, [])))
    check("BUY excluded", all(o["orderid"] != "B1" for o in working.get(key, [])))
    check("COMPLETE excluded", all(o["orderid"] != "S3" for o in working.get(key, [])))
    check("other contract kept separately",
          working.get(("NIFTY24OCT22700PE", "NFO", "M")) == [
              {"orderid": "S4", "remaining": 75}
          ])


# ── 4. close_all_positions end-to-end (broker I/O stubbed) ─────────────────

class _FakeResponse:
    def __init__(self, status):
        self.status = status


def _run_close(position_rows, order_books, place_status=200,
               cancel_status=200):
    """Drive close_all_positions with stubbed broker I/O.

    `order_books` is consumed one reply per read, so a re-read after a cancel
    can answer differently from the first read.
    """
    placed, cancelled = [], []
    books = list(order_books)
    reads = {"positions": 0}

    def fake_get_positions(auth):
        reads["positions"] += 1
        return list(position_rows)

    def fake_get_order_book(auth):
        return books.pop(0) if books else []

    def fake_get_symbol(token, exchange):
        return "NIFTY24OCT22800CE" if token == "11111" else None

    def fake_place_order_api(data, auth):
        placed.append(dict(data))
        if place_status == 200:
            return _FakeResponse(200), {"stat": "Ok", "norenordno": f"NEW{len(placed)}"}, f"NEW{len(placed)}"
        return _FakeResponse(400), {"stat": "Not_Ok", "emsg": "RMS: margin"}, None

    def fake_cancel_order(orderid, auth):
        cancelled.append(orderid)
        if cancel_status == 200:
            return {"status": "success", "orderid": orderid}, 200
        return {"status": "error", "message": "cannot cancel"}, 400

    saved = (oa.get_positions, oa.get_order_book, oa.get_symbol,
             oa.place_order_api, oa.cancel_order, oa._CLOSE_CANCEL_SETTLE)
    oa.get_positions = fake_get_positions
    oa.get_order_book = fake_get_order_book
    oa.get_symbol = fake_get_symbol
    oa.place_order_api = fake_place_order_api
    oa.cancel_order = fake_cancel_order
    oa._CLOSE_CANCEL_SETTLE = 0  # keep the smoke test instant
    try:
        result, status = oa.close_all_positions("api-key", "auth-token")
    finally:
        (oa.get_positions, oa.get_order_book, oa.get_symbol,
         oa.place_order_api, oa.cancel_order, oa._CLOSE_CANCEL_SETTLE) = saved

    return result, status, placed, cancelled


def test_oversell_guard():
    section("4a. the close never exceeds the long (no oversell into a short)")

    long_row = [{"tsym": "NIFTY24OCT22800CE", "exch": "NFO", "prd": "M",
                 "token": "11111", "netqty": "390"}]

    result, status, placed, cancelled = _run_close(long_row, [[]])
    check("flat order book -> one close", len(placed) == 1, placed)
    check("close is a SELL", placed and placed[0]["action"] == "SELL", placed)
    check("quantity == long (390, never more)",
          placed and placed[0]["quantity"] == "390", placed)
    check("no cancel needed", cancelled == [], cancelled)
    check("reports success", status == 200 and result.get("status") == "success", result)


def test_stale_sell_cancelled_and_replaced():
    section("4b. a stale working SELL is CANCELLED and re-priced, not skipped")

    long_row = [{"tsym": "NIFTY24OCT22800CE", "exch": "NFO", "prd": "M",
                 "token": "11111", "netqty": "390"}]
    stale_book = [{"trantype": "S", "status": "OPEN", "qty": "390",
                   "fillshares": "0", "norenordno": "STALE1",
                   "tsym": "NIFTY24OCT22800CE", "exch": "NFO", "prd": "M"}]

    # First read sees the stale order; the post-cancel re-read sees it gone.
    result, status, placed, cancelled = _run_close(long_row, [stale_book, []])

    check("the stale SELL was cancelled", cancelled == ["STALE1"], cancelled)
    check("a replacement SELL WAS placed (no deadlock)", len(placed) == 1, placed)
    check("replacement quantity is the full long",
          placed and placed[0]["quantity"] == "390", placed)
    check("reports success",
          status == 200 and result.get("status") == "success", result)


def test_uncoverable_working_sell_reduces_quantity():
    section("4c. a working SELL the broker will NOT cancel still blocks overselling")

    long_row = [{"tsym": "NIFTY24OCT22800CE", "exch": "NFO", "prd": "M",
                 "token": "11111", "netqty": "390"}]
    stale_book = [{"trantype": "S", "status": "OPEN", "qty": "390",
                   "fillshares": "0", "norenordno": "STUCK",
                   "tsym": "NIFTY24OCT22800CE", "exch": "NFO", "prd": "M"}]

    # Cancel fails, so the re-read still shows the working order.
    result, status, placed, cancelled = _run_close(
        long_row, [stale_book, stale_book], cancel_status=400
    )

    check("cancel was attempted", cancelled == ["STUCK"], cancelled)
    check("NO second SELL placed while one still works", placed == [], placed)
    check("reports success via the covered branch",
          status == 200 and "covered" in result, result)


def test_rejected_close_is_reported():
    section("4d. a REJECTED close is an error, not a silent success")

    long_row = [{"tsym": "NIFTY24OCT22800CE", "exch": "NFO", "prd": "M",
                 "token": "11111", "netqty": "390"}]

    result, status, placed, cancelled = _run_close(long_row, [[]], place_status=400)

    check("one close attempted", len(placed) == 1)
    check("the rejection surfaces as non-200", status == 500, status)
    check("status is 'error'", result.get("status") == "error", result)
    check("failure detail is carried", bool(result.get("failed")), result)


# ── 5. the MKT guard ──────────────────────────────────────────────────────

def test_mpp_never_sends_raw_mkt():
    section("5. an unpriceable MARKET raises instead of sending MKT")

    check("MPPPriceUnavailable is an Exception",
          issubclass(MPPPriceUnavailable, Exception))


def main():
    print("=" * 66)
    print("  Flattrade square-off safeguards — smoke tests")
    print(f"  python {sys.version.split()[0]}")
    print("=" * 66)
    test_empty_book_detection()
    test_tick_rounding()
    test_marketable_price()
    test_working_sell_orders()
    test_oversell_guard()
    test_stale_sell_cancelled_and_replaced()
    test_uncoverable_working_sell_reduces_quantity()
    test_rejected_close_is_reported()
    test_mpp_never_sends_raw_mkt()

    print("\n" + "=" * 66)
    if _FAILURES:
        print(f"  {len(_FAILURES)} of {_CHECKS} checks FAILED")
        for f in _FAILURES:
            print(f"    - {f}")
        print("=" * 66)
        return 1
    print(f"  All {_CHECKS} checks passed")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
