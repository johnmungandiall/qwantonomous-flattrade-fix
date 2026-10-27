"""Smoke tests for the mStock square-off / position-read safeguards.

Run with the server's OWN virtualenv (the ambient Python lacks httpx):

    cd C:\\dev\\backend-installer\\payload\\server
    .\\.venv\\Scripts\\python.exe test\\test_mstock_squareoff_safeguards.py

Exit code 0 = every check passed; 1 = at least one failed.

These mirror the Flattrade safeguards (test/test_flattrade_squareoff_safeguards.py)
for mStock's own response shapes, which differ from Noren's:

  mStock answers success as {"status": "true"} on the order endpoints but
  {"status": "True"} in the vendor's own mock order book, and a failure as
  {"status": "false", "message": ..., "data": null}. get_api_response returns
  {} when the payload cannot be decoded, and the position/order rows carry
  `quantity` / `filledshares` / `orderid` / `symboltoken` / `producttype`.

WHAT THIS PINS
  1. An UNREADABLE book never reads as "flat" - it raises BrokerReadError, so a
     smart order cannot act on a book it cannot see.
  2. get_open_position SUMS every matching row (a contract can appear more than
     once) and reaches an NFO contract from mStock's NSE-labelled row.
  3. A stale working SELL is CANCELLED and the close RE-PRICED, not skipped.
  4. The close quantity never exceeds the long, so a square-off cannot be sold
     through zero into a SHORT.
  5. A REJECTED close is an error, not a silent "All Open Positions SquaredOff".
  6. Order validity: IOC is honoured, an absent/garbage value stays DAY.
"""

import os
import sys

SERVER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

import broker.mstock.api.order_api as oa  # noqa: E402
from broker.mstock.mapping.transform_data import _duration  # noqa: E402

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
    section("1. a FAILED read RAISES; an empty book is []")

    ok_empty = {"status": "true", "message": "SUCCESS", "data": []}
    check("data: [] -> empty", oa._read_book(ok_empty, "book") == [])

    ok_null = {"status": "true", "message": "SUCCESS", "data": None}
    check("data: null on success -> empty", oa._read_book(ok_null, "book") == [])

    no_data = {"status": "false", "message": "No data found", "data": None}
    check("'no data' message -> empty", oa._read_book(no_data, "book") == [])

    for label, reply in [
        ("suspended API", {"status": "false", "message": "API is suspended/expired"}),
        ("invalid token", {"status": "false", "message": "Invalid Token"}),
        ("errorcode only", {"status": "false", "errorcode": "IA403"}),
        ("bare {}", {}),
    ]:
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

    check("list reply keeps only dict rows",
          oa._read_book([{"netqty": "75"}, "junk", {"netqty": "0"}], "b")
          == [{"netqty": "75"}, {"netqty": "0"}])

    # mStock never answers a book with a bare row: get_positions/get_order_book
    # always return the {"status", "data"} envelope, and get_api_response
    # returns {} when the payload cannot be decoded. So a status-less dict is
    # treated as a READ FAILURE (fail closed) rather than guessed as one row -
    # guessing is how a garbled reply becomes a phantom position.
    raised = False
    try:
        oa._read_book({"netqty": "1"}, "b")
    except oa.BrokerReadError:
        raised = True
    check("a status-less dict RAISES (fail closed, never a guessed row)", raised)


def test_status_spellings():
    section("1b. every success spelling mStock uses is accepted")

    for value in [True, "true", "True", "success", "SUCCESS"]:
        check(f"{value!r} is success", oa._is_success(value) is True)
    for value in [False, "false", "False", None, "", "rejected"]:
        check(f"{value!r} is NOT success", oa._is_success(value) is False)

    check("_status_ok accepts the mock's \"True\"",
          oa._status_ok({"status": "True"}) is True)
    check("_status_ok rejects a failure dict",
          oa._status_ok({"status": "false"}) is False)


# ── 2. get_open_position ───────────────────────────────────────────────────

def _with_open_position_stubs(rows, token="11111", fn=None):
    """Run fn with get_token / position cache stubbed."""
    saved = (oa.get_token, oa._get_cached_positions)
    oa.get_token = lambda symbol, exchange: token
    oa._get_cached_positions = lambda auth: rows
    try:
        return fn()
    finally:
        oa.get_token, oa._get_cached_positions = saved


def test_open_position():
    section("2. get_open_position SUMS matching rows and reaches NFO")

    two_rows = {"status": "true", "data": [
        {"symboltoken": "11111", "exchange": "NSE", "instrumenttype": "OPTIDX",
         "producttype": "CARRYFORWARD", "netqty": "200"},
        {"symboltoken": "11111", "exchange": "NSE", "instrumenttype": "OPTIDX",
         "producttype": "CARRYFORWARD", "netqty": "190"},
    ]}
    qty = _with_open_position_stubs(
        two_rows,
        fn=lambda: oa.get_open_position("NIFTY24OCT22800CE", "NFO", "CARRYFORWARD", "t"),
    )
    check("two rows for one contract SUM (200 + 190)", qty == "390", qty)

    nse_labelled = {"status": "true", "data": [
        {"symboltoken": "11111", "exchange": "NSE", "instrumenttype": "OPTIDX",
         "producttype": "CARRYFORWARD", "netqty": "390"},
    ]}
    qty = _with_open_position_stubs(
        nse_labelled,
        fn=lambda: oa.get_open_position("NIFTY24OCT22800CE", "NFO", "CARRYFORWARD", "t"),
    )
    check("mStock's NSE-labelled derivative is found via openalgo exchange NFO",
          qty == "390", qty)

    other_contract = {"status": "true", "data": [
        {"symboltoken": "99999", "exchange": "NSE", "instrumenttype": "OPTIDX",
         "producttype": "CARRYFORWARD", "netqty": "390"},
    ]}
    qty = _with_open_position_stubs(
        other_contract,
        fn=lambda: oa.get_open_position("NIFTY24OCT22800CE", "NFO", "CARRYFORWARD", "t"),
    )
    check("a different contract reads flat", qty == "0", qty)

    def unreadable():
        return oa.get_open_position("NIFTY24OCT22800CE", "NFO", "CARRYFORWARD", "t")

    raised = False
    try:
        _with_open_position_stubs(
            {"status": "false", "message": "Session expired"}, fn=unreadable
        )
    except oa.BrokerReadError:
        raised = True
    check("an UNREADABLE book RAISES (never '0' flat)", raised)


# ── 3. working-SELL accounting ─────────────────────────────────────────────

def test_working_sell_orders():
    section("3. _working_sell_orders reports what is still working")

    key = ("11111", "NSE", "CARRYFORWARD")
    book = [
        # working SELL, half filled -> 200 still working
        {"transactiontype": "SELL", "status": "Open", "quantity": "390",
         "filledshares": "190", "orderid": "S1", "symboltoken": "11111",
         "exchange": "NSE", "producttype": "CARRYFORWARD"},
        # fully filled, still "Open" -> 0 working, ignored
        {"transactiontype": "SELL", "status": "Open", "quantity": "390",
         "filledshares": "390", "orderid": "S2", "symboltoken": "11111",
         "exchange": "NSE", "producttype": "CARRYFORWARD"},
        # a BUY is never a square-off SELL
        {"transactiontype": "BUY", "status": "Open", "quantity": "390",
         "filledshares": "0", "orderid": "B1", "symboltoken": "11111",
         "exchange": "NSE", "producttype": "CARRYFORWARD"},
        # already complete
        {"transactiontype": "SELL", "status": "Traded", "quantity": "390",
         "filledshares": "0", "orderid": "S3", "symboltoken": "11111",
         "exchange": "NSE", "producttype": "CARRYFORWARD"},
        # a different contract must not leak in
        {"transactiontype": "SELL", "status": "Open", "quantity": "75",
         "filledshares": "0", "orderid": "S4", "symboltoken": "22222",
         "exchange": "NSE", "producttype": "CARRYFORWARD"},
    ]
    working = oa._working_sell_orders(book)

    check("only the working SELL, with its order id",
          working.get(key) == [{"orderid": "S1", "remaining": 200}], working.get(key))
    check("fully-filled SELL excluded",
          all(o["orderid"] != "S2" for o in working.get(key, [])))
    check("BUY excluded", all(o["orderid"] != "B1" for o in working.get(key, [])))
    check("TRADED excluded", all(o["orderid"] != "S3" for o in working.get(key, [])))
    check("other contract kept separately",
          working.get(("22222", "NSE", "CARRYFORWARD")) == [
              {"orderid": "S4", "remaining": 75}
          ])


# ── 4. close_all_positions end-to-end (broker I/O stubbed) ─────────────────

class _FakeResponse:
    def __init__(self, status):
        self.status = status


def _run_close(position_reply, order_books, place_status=200, cancel_status=200,
               symbol="NIFTY24OCT22800CE"):
    """Drive close_all_positions with stubbed broker I/O.

    `order_books` is consumed one reply per read, so a re-read after a cancel
    can answer differently from the first read.
    """
    placed, cancelled = [], []
    books = list(order_books)

    def fake_get_positions(auth):
        return position_reply

    def fake_get_order_book(auth):
        return books.pop(0) if books else {"status": "true", "data": []}

    def fake_get_symbol(token, exchange):
        return symbol if str(token) == "11111" else None

    def fake_place_order_api(data, auth):
        placed.append(dict(data))
        if place_status == 200:
            return (
                _FakeResponse(200),
                {"status": "true", "message": "SUCCESS",
                 "data": {"orderid": f"NEW{len(placed)}"}},
                f"NEW{len(placed)}",
            )
        return _FakeResponse(400), {"status": "false", "message": "RMS: margin"}, None

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


LONG_ROW = {"symboltoken": "11111", "exchange": "NSE", "instrumenttype": "OPTIDX",
            "producttype": "CARRYFORWARD", "netqty": "390",
            "symbolname": "NIFTY24OCT22800CE"}
LONG_REPLY = {"status": "true", "message": "SUCCESS", "data": [LONG_ROW]}

STALE_SELL = {"transactiontype": "SELL", "status": "Open", "quantity": "390",
              "filledshares": "0", "orderid": "STALE1", "symboltoken": "11111",
              "exchange": "NSE", "producttype": "CARRYFORWARD"}
STALE_BOOK = {"status": "true", "data": [STALE_SELL]}
EMPTY_BOOK = {"status": "true", "data": []}


def test_flat_book():
    section("4a. a flat / empty book is a 200 no-op (not a crash)")

    for label, reply in [
        ("data: []", {"status": "true", "data": []}),
        ("data: null", {"status": "true", "data": None}),
        ("'no data' failure", {"status": "false", "message": "No data found"}),
    ]:
        result, status, placed, cancelled = _run_close(reply, [])
        check(f"{label} -> 200 no-op", status == 200 and not placed, (status, result))


def test_unreadable_book():
    section("4b. an UNREADABLE book is a 500, never a false 'squared off'")

    result, status, placed, cancelled = _run_close(
        {"status": "false", "message": "API is suspended/expired"}, []
    )
    check("status 500", status == 500, status)
    check("reports an error", result.get("status") == "error", result)
    check("nothing placed", placed == [], placed)


def test_oversell_guard():
    section("4c. the close never exceeds the long (no oversell into a short)")

    result, status, placed, cancelled = _run_close(LONG_REPLY, [EMPTY_BOOK])
    check("flat order book -> one close", len(placed) == 1, placed)
    check("close is a SELL", placed and placed[0]["action"] == "SELL", placed)
    check("quantity == long (390, never more)",
          placed and placed[0]["quantity"] == "390", placed)
    check("product mapped CARRYFORWARD -> NRML",
          placed and placed[0]["product"] == "NRML", placed)
    check("no cancel needed", cancelled == [], cancelled)
    check("reports success", status == 200 and result.get("status") == "success", result)


def test_stale_sell_cancelled_and_replaced():
    section("4d. a stale working SELL is CANCELLED and re-priced, not skipped")

    # First read sees the stale order; the post-cancel re-read sees it gone.
    result, status, placed, cancelled = _run_close(LONG_REPLY, [STALE_BOOK, EMPTY_BOOK])

    check("the stale SELL was cancelled", cancelled == ["STALE1"], cancelled)
    check("a replacement SELL WAS placed (no deadlock)", len(placed) == 1, placed)
    check("replacement quantity is the full long",
          placed and placed[0]["quantity"] == "390", placed)
    check("reports success", status == 200 and result.get("status") == "success", result)


def test_uncoverable_working_sell_reduces_quantity():
    section("4e. a working SELL the broker will NOT cancel still blocks overselling")

    # Cancel fails, so the re-read still shows the working order.
    result, status, placed, cancelled = _run_close(
        LONG_REPLY, [STALE_BOOK, STALE_BOOK], cancel_status=400
    )

    check("cancel was attempted", cancelled == ["STALE1"], cancelled)
    check("NO second SELL placed while one still works", placed == [], placed)
    check("reports success via the covered branch",
          status == 200 and "covered" in result, result)


def test_partial_working_sell_reduces_quantity():
    section("4f. a partially-covered long sells only the uncovered remainder")

    partial = dict(STALE_SELL)
    partial["filledshares"] = "290"  # 100 still working
    partial_book = {"status": "true", "data": [partial]}

    result, status, placed, cancelled = _run_close(
        LONG_REPLY, [partial_book, partial_book], cancel_status=400
    )
    check("one close placed", len(placed) == 1, placed)
    check("quantity reduced by the working 100 (390 -> 290)",
          placed and placed[0]["quantity"] == "290", placed)


def test_rejected_close_is_reported():
    section("4g. a REJECTED close is an error, not a silent success")

    result, status, placed, cancelled = _run_close(LONG_REPLY, [EMPTY_BOOK], place_status=400)

    check("one close attempted", len(placed) == 1)
    check("the rejection surfaces as non-200", status == 500, status)
    check("status is 'error'", result.get("status") == "error", result)
    check("failure detail is carried", bool(result.get("failed")), result)


def test_missing_symbol_is_reported():
    section("4h. an unresolvable symbol is a FAILURE, not a silent skip")

    result, status, placed, cancelled = _run_close(
        LONG_REPLY, [EMPTY_BOOK], symbol=None
    )
    check("no order placed", placed == [], placed)
    check("status 500", status == 500, status)
    check("failure names the symbol",
          bool(result.get("failed")) and result["failed"][0]["message"] == "symbol lookup failed",
          result)


class _FakeHttpResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class _FakeHttpClient:
    """Stands in for get_httpx_client() so this test never touches the network."""

    def __init__(self, reply='{"status": "true", "message": "SUCCESS"}', status_code=200):
        self._reply = reply
        self._status = status_code
        self.calls = []

    def post(self, url, headers=None, content=None, json=None):
        self.calls.append(url)
        return _FakeHttpResponse(self._reply, self._status)


def test_cancel_all_failed_read_is_not_a_crash():
    section("4i. a failed order-book read cannot crash or fake cancel-all")

    saved_get, saved_client = oa.get_order_book, oa.get_httpx_client

    def run(label, reply, expected_cancelled, expected_failed):
        client = _FakeHttpClient()
        oa.get_order_book = lambda auth, _r=reply: _r
        oa.get_httpx_client = lambda: client
        try:
            cancelled, failed = oa.cancel_all_orders_api({}, "auth-token")
        finally:
            oa.get_order_book, oa.get_httpx_client = saved_get, saved_client
        check(f"{label}: no crash", True)
        check(
            f"{label}: cancelled={expected_cancelled} failed={expected_failed}",
            cancelled == expected_cancelled and failed == expected_failed,
            (cancelled, failed),
        )
        return client

    # A failed read is neither a pending order nor a crash.
    run("bare {} reply", {}, [], [])
    run("failure dict", {"status": "false", "message": "Invalid Token"}, [], [])
    run("'no data'", {"status": "false", "message": "No data found"}, [], [])

    # A real reply carrying one WORKING order is cancelled and reported.
    client = run(
        "list reply with one working order",
        [{"orderid": "X1", "status": "Open"}],
        ["X1"],
        [],
    )
    check("the cancelall endpoint WAS called for a working order",
          len(client.calls) == 1, client.calls)

    # An order that can no longer fill is not sent to the broker at all.
    client = run(
        "completed order only",
        {"status": "true", "data": [{"orderid": "X2", "status": "Traded"}]},
        [],
        [],
    )
    check("no broker call when nothing is working", client.calls == [], client.calls)


# ── 5. order validity ──────────────────────────────────────────────────────

def test_duration():
    section("5. validity: IOC honoured, absent/garbage stays DAY")

    check("absent -> DAY", _duration({}) == "DAY")
    check("None -> DAY", _duration({"validity": None}) == "DAY")
    check("IOC -> IOC", _duration({"validity": "IOC"}) == "IOC")
    check("lowercase ioc -> IOC", _duration({"validity": "ioc"}) == "IOC")
    check("DAY -> DAY", _duration({"validity": "day"}) == "DAY")
    check("garbage clamped to DAY", _duration({"validity": "GTC"}) == "DAY")
    check("empty string -> DAY", _duration({"validity": "   "}) == "DAY")


def main():
    print("=" * 66)
    print("  mStock square-off safeguards — smoke tests")
    print(f"  python {sys.version.split()[0]}")
    print("=" * 66)
    test_empty_book_detection()
    test_status_spellings()
    test_open_position()
    test_working_sell_orders()
    test_flat_book()
    test_unreadable_book()
    test_oversell_guard()
    test_stale_sell_cancelled_and_replaced()
    test_uncoverable_working_sell_reduces_quantity()
    test_partial_working_sell_reduces_quantity()
    test_rejected_close_is_reported()
    test_missing_symbol_is_reported()
    test_cancel_all_failed_read_is_not_a_crash()
    test_duration()

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
