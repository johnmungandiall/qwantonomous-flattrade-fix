# Flattrade Square-off & Cancel-all — response-shape hardening

**Date:** 2026-10-22 (IST)
**Branch:** `main`
**App version at time of writing:** `2.0.2.1` (`utils/version.py#VERSION`)
**Status:** committed locally on `main`, **not released**. No tag, no deploy, no push was part of this work.
**Author of record:** John Mungandi

---

## 1. Executive summary

Two production defects were fixed in the Flattrade broker plugin, both in
`broker/flattrade/api/order_api.py`. They are the *same defect wearing two
names*:

> A broker helper's reply was used as if it were always the documented success
> **list**, without type-checking it first. A failed or empty read does not
> return that list — it returns a bare `{"stat": "Not_Ok", "emsg": ...}`
> **dict**. Indexing or iterating that dict raises, and the OpenAlgo service
> layer above catches everything and converts it into an HTTP 500.

The consequences were user-visible and, in one case, operationally dangerous:

| # | Function | Python error | What the user saw | Real risk |
|---|---|---|---|---|
| 1 | `close_all_positions` (square-off) | `KeyError: 0` | "Failed to close positions due to internal error" on a **flat** account | Callers retried 3×, a client's `P.Cross` side switch stayed held |
| 2 | `cancel_all_orders_api` (cancel-all) | `AttributeError: 'str' object has no attribute 'get'` | "Failed to cancel all orders due to internal error" with nothing pending | Cosmetic — caller logged it and still squared off |

Fix #1 is a **correctness** fix: a flat account must not report a failed
square-off. Fix #2 is a **noise/correctness** fix: a read failure must not be
reported as a cancellation failure.

Both fixes normalise the response shapes at the broker boundary and return the
broker's *documented* "nothing to do" answer, so the service layer no longer
turns a no-op into a 500.

---

## 2. Timeline of the day's commits

| Time (IST) | Commit | Type | Subject |
|---|---|---|---|
| 10:24 | `3e79860` | chore | Initial commit — the whole Qwantonomous/OpenAlgo tree (2,440 files, 713,760 insertions) |
| 10:26 | `24161aa` | **fix** | `fix(flattrade): flat position book crashed square-off into HTTP 500` — 1 file, +25/−4 |
| 10:27 | `7e84e36` | docs | `docs(kb): record the local flattrade close_all_positions patch` — `kb/gotchas.md` +1 |
| 10:52 | `4d662f5` | **fix** | `fix(flattrade): failed OrderBook read crashed cancel-all into HTTP 500` — 1 file, +11/−1 |
| 10:52 | `05244b7` | docs | `docs(kb): record the local cancel_all_orders_api patch` — `kb/gotchas.md` +1 |

Working tree was clean at the end of the day; no uncommitted files, no stray
scratch files, no plan file left behind.

---

## 3. Defect 1 — a flat position book crashed the square-off

### 3.1 Symptom

Triggering square-off ("close all positions") on a Flattrade account with **no
open positions** returned:

```
HTTP 500
{"status": "error", "message": "Failed to close positions due to internal error"}
```

on the API *and* in the app UI. Nothing was actually wrong with the account —
there was simply nothing to close. Because callers treat a 500 as a real
failure, they retried (3 attempts on every exit path), and the client's
`P.Cross` side switch was held in the "closing" state because the close never
reported success.

### 3.2 Live evidence (real log, not reconstructed)

Source: `log/errors.jsonl` (1,026 lines at time of inspection).

The first occurrence, 2026-10-21 09:15:00, with the full traceback:

```
{"ts": "2026-10-21 09:15:00", "level": "ERROR",
 "logger": "services.close_position_service",
 "message": "Error in broker_module.close_all_positions: 0"}

Traceback (most recent call last):
  File "...\services\close_position_service.py", line 141, in close_position_with_auth
    response_code, status_code = broker_module.close_all_positions(api_key, auth_token)
  File "...\broker\flattrade\api\order_api.py", line 278, in close_all_positions
    if positions_response is None or positions_response[0]["stat"] == "Not_Ok":
                                     ~~~~~~~~~~~~~~~~~~^^^
KeyError: 0
```

It repeated at 09:15:01 and 09:15:02 (the 3 retries), and again the next
morning at **2026-10-22 09:14:59** — i.e. it was still live roughly an hour
before the fix landed at 10:26.

### 3.3 Root cause chain

1. `broker/flattrade/api/order_api.py#get_positions` calls `get_api_response("/PiConnectAPI/PositionBook", ...)`.
2. `get_api_response` returns `json.loads(response.text)` — the raw broker JSON, whatever shape it is.
3. Flattrade's documented failure shape is a **bare object**:
   `{"stat": "Not_Ok", "emsg": "..."}` — *not* a list.
4. A genuinely flat account can produce any of: `None`, `[]`, a bare `Not_Ok` dict,
   or a one-element list whose only item is the `Not_Ok` dict.
5. The old guard was:
   ```python
   if positions_response is None or positions_response[0]["stat"] == "Not_Ok":
   ```
   - `None` → handled.
   - `[]` → `[0]` raises `IndexError`.
   - bare `Not_Ok` dict → `[0]` raises **`KeyError: 0`** ← this is the traceback we have.
6. The exception escapes to `services/close_position_service.py#close_position_with_auth`, whose `try/except Exception` logs it and returns
   `(False, {"status": "error", "message": "Failed to close positions due to internal error"}, 500)`
   and publishes a `PositionClosedEvent` with a failure message on the event bus.
7. The caller sees a failure for an operation that had nothing to do.

### 3.4 Code — before

```python
def close_all_positions(current_api_key, auth):
    AUTH_TOKEN = auth
    positions_response = get_positions(AUTH_TOKEN)

    # Check if the positions data is null or empty
    if positions_response is None or positions_response[0]["stat"] == "Not_Ok":
        return {"message": "No Open Positions Found"}, 200

    if positions_response:
        for position in positions_response:
            if int(position["netqty"]) == 0:
                continue
            ...
```

### 3.5 Code — after (current `main`)

```python
def close_all_positions(current_api_key, auth):
    AUTH_TOKEN = auth
    positions_response = get_positions(AUTH_TOKEN)

    # A flat book does NOT always come back as an empty list: the broker can
    # answer with nothing at all, an empty list, or a bare
    # {"stat": "Not_Ok", "emsg": ...} dict when there is no position data.
    if not positions_response:
        return {"message": "No Open Positions Found"}, 200

    if isinstance(positions_response, dict):
        positions = [positions_response]
    else:
        positions = list(positions_response)

    if (
        positions
        and isinstance(positions[0], dict)
        and positions[0].get("stat") == "Not_Ok"
    ):
        return {"message": "No Open Positions Found"}, 200

    if positions:
        for position in positions:
            ...  # unchanged: netqty 0 skipped, SELL if long, BUY if short
```

Note the deliberate details:

- `if not positions_response` catches **both** `None` and `[]` before any indexing.
- The dict→list normalisation means the downstream loop only ever walks real rows.
- `.get("stat")` instead of `["stat"]` — a row without a `stat` key no longer raises.
- The success path (`{"status": "success", "message": "All Open Positions SquaredOff"}, 200`) was left exactly as it was.

### 3.6 Behaviour matrix (verified, see §6)

| `get_positions()` reply | Before | After |
|---|---|---|
| `None` | 200 "No Open Positions Found" | 200 "No Open Positions Found" |
| `[]` (empty list) | **`IndexError` → HTTP 500** | 200 "No Open Positions Found" |
| `{}` (empty dict) | **`KeyError: 0` → HTTP 500** | 200 "No Open Positions Found" |
| `{"stat": "Not_Ok", "emsg": "..."}` | **`KeyError: 0` → HTTP 500** | 200 "No Open Positions Found" |
| `[{"stat": "Not_Ok", ...}]` | 200 "No Open Positions Found" | 200 "No Open Positions Found" |
| `[{... "netqty": "0" ...}]` | no order placed | no order placed (skipped) |
| `[{... "netqty": "260" ...}]` | one SELL 260 | one SELL 260 |
| `[{... "netqty": "-65" ...}]` | one BUY 65 | one BUY 65 |

---

## 4. Defect 2 — a failed order book read crashed cancel-all

### 4.1 Symptom

"Cancel all orders" returned:

```
HTTP 500
{"status": "error", "message": "Failed to cancel all orders due to internal error"}
```

even though there was nothing pending to cancel. Client-side this was
**non-fatal** — the app logged it and carried on to square off — so it was
noise rather than a blocked operation. It still poisoned the error log and made
a healthy account look broken.

### 4.2 Live evidence

Same log file, same second as the square-off failure:

```
{"ts": "2026-10-21 09:15:00", "level": "ERROR",
 "logger": "services.cancel_all_order_service",
 "message": "Error in broker_module.cancel_all_orders_api: 'str' object has no attribute 'get'"}

Traceback (most recent call last):
  File "...\services\cancel_all_order_service.py", line 131, in cancel_all_orders_with_auth
    canceled_orders, failed_cancellations = broker_module.cancel_all_orders_api(order_data, auth_token)
  File "...\broker\flattrade\api\order_api.py", line 404, in cancel_all_orders_api
    if normalize_order_status(order.get("status")) == "open"
                              ^^^^^^^^^
AttributeError: 'str' object has no attribute 'get'
```

The app log additionally recorded `cancelAllOrders failed: HTTP 500`.

### 4.3 Root cause chain

1. `get_order_book()` → `get_api_response("/PiConnectAPI/OrderBook", ...)` → raw JSON.
2. On a **failed** read the reply is a bare `{"stat": "Not_Ok", "emsg": ...}` dict, not a list.
3. The old code only guarded `None`, then immediately ran a list comprehension over the reply:
   ```python
   orders_to_cancel = [order for order in order_book_response
                       if normalize_order_status(order.get("status")) == "open"]
   ```
4. Iterating a **dict** yields its **string keys** — `"stat"`, `"emsg"` — so `order` is the
   string `"stat"`, and `"stat".get(...)` raises `AttributeError: 'str' object has no attribute 'get'`.
5. `services/cancel_all_order_service.py#cancel_all_orders_with_auth` catches it and returns
   `(False, {"status": "error", "message": "Failed to cancel all orders due to internal error"}, 500)`,
   publishing an `OrderFailedEvent` with the exception text.

### 4.4 Code — before

```python
def cancel_all_orders_api(data, auth):
    AUTH_TOKEN = auth
    order_book_response = get_order_book(AUTH_TOKEN)

    if order_book_response is None:
        return [], []  # failure to retrieve the order book

    orders_to_cancel = [
        order
        for order in order_book_response
        if normalize_order_status(order.get("status")) == "open"
    ]
    ...
```

### 4.5 Code — after (current `main`)

```python
def cancel_all_orders_api(data, auth):
    AUTH_TOKEN = auth
    order_book_response = get_order_book(AUTH_TOKEN)

    if order_book_response is None:
        return [], []

    # A FAILED OrderBook read does not come back as a list: it is a bare
    # {"stat": "Not_Ok", "emsg": ...} dict.
    if not isinstance(order_book_response, list):
        return [], []

    orders_to_cancel = [
        order
        for order in order_book_response
        if isinstance(order, dict)
        and normalize_order_status(order.get("status")) == "open"
    ]
    ...
```

Two independent guards, each closing a different hole:

- `isinstance(order_book_response, list)` — the reply itself is not a list (the reported bug).
- `isinstance(order, dict)` per item — the list contains a non-dict element (defensive,
  same family; the `norenordno` / `status` reads below assume dicts).

The return shape `(canceled_orders, failed_cancellations)` was **not** changed,
so `cancel_all_orders_with_auth` needed no edit and its success message
(`"Canceled N orders. Failed to cancel M orders."`) is unchanged.

### 4.6 Behaviour matrix (verified, see §6)

| `get_order_book()` reply | Before | After |
|---|---|---|
| `None` | `([], [])` | `([], [])` |
| `{"stat": "Not_Ok", "emsg": "..."}` | **`AttributeError` → HTTP 500** | `([], [])`, no crash |
| `[]` | `([], [])` | `([], [])` |
| `["some-string"]` | **`AttributeError`** | `([], [])` |
| one OPEN order `O1` | `(['O1'], [])` | `(['O1'], [])` |
| one OPEN + one COMPLETE order | cancels only the OPEN one | cancels only the OPEN one |

---

## 5. The rule both fixes teach

> **Never iterate or index a broker helper's reply before type-checking it.
> The documented success payload is not what you get on failure — you get the
> broker's error object, and OpenAlgo's service layer will report your
> `TypeError` as a failed trading operation.**

Practical checklist for every `broker/*/api/order_api.py` function that reads a
book (PositionBook, OrderBook, Holdings, TradeBook):

1. Guard `falsy` (`None` **and** empty) before indexing.
2. Guard `isinstance(reply, list)` before iterating.
3. Guard `isinstance(item, dict)` before `item.get(...)` / `item[...]`.
4. Use `.get()` for optional keys like `stat`; keep `[...]` only for keys the
   documented success row guarantees (`netqty`, `token`, `exch`, `norenordno`).
5. Return the broker's own documented "nothing to do" answer, so the service
   layer produces a 200 and a truthful message.

---

## 6. Verification performed

No test file was added — the two functions are not covered by the existing
pytest suite (searched `test/` for `close_all_positions` / `cancel_all_orders` /
`flattrade`: the only matches are Flow-executor methods in
`test/test_flow_qa_regressions.py` and a tool-name list in
`test/test_mcp_integrity.py`, neither of which touches the broker functions).
That is a gap worth closing — see §9.

Verification was therefore done by **executing the real function text**:

1. The current function source was extracted from the live file with `ast`
   (source segment of the real definition, not a hand-copied mock).
2. That segment was executed with the broker/network helpers stubbed
   (`get_positions`, `get_order_book`, `place_order_api`, `cancel_order`,
   `get_symbol`, `reverse_map_product_type`, `normalize_order_status`).
3. Every input in the §3.6 and §4.6 matrices was fed through it.
4. A pre-fix copy of `close_all_positions` was run on the same inputs to prove
   the defect was real and the fix is what removes it — it raised `KeyError` on
   `{}` and on the bare `Not_Ok` dict, and `IndexError` on `[]`.

Results: all "after" columns above passed; the pre-fix backup failed exactly as
the production traceback showed.

**Not run:** the project's declared check command
`pre-commit run --all-files` is **not installed on this machine**, so it could
not be run and is not claimed. `pytest` is available and was used only for
targeted checks, never the full suite.

---

## 7. Files touched

| File | Change | Commit |
|---|---|---|
| `broker/flattrade/api/order_api.py#close_all_positions` | +25 / −4 — response-shape normalisation, 200 no-op for a flat book | `24161aa` |
| `broker/flattrade/api/order_api.py#cancel_all_orders_api` | +11 / −1 — list-type guard + per-item dict guard | `4d662f5` |
| `kb/gotchas.md` | +1 line each — two LOCAL PATCH entries, both citing `path#symbol` | `7e84e36`, `05244b7` |

Nothing else was modified: no service-layer change, no route change, no
frontend change, no dependency change, no version bump (still `2.0.2.1`).

The service-layer contract stayed fixed throughout:

- `services/close_position_service.py#close_position_with_auth` — broker 200 ⇒
  `{"status": "success", "message": "All Open Positions Squared Off"}, 200`;
  any broker exception ⇒ HTTP 500 "Failed to close positions due to internal error".
- `services/cancel_all_order_service.py#cancel_all_orders_with_auth` — any
  broker exception ⇒ HTTP 500 "Failed to cancel all orders due to internal error".

That is precisely why the broker-level return values matter so much: the
service layer trusts them blindly.

---

## 8. Cross-broker audit of the same defect family

Read from the live tree at the time of this report (not from memory):

| Broker | `close_all_positions` | `cancel_all_orders_api` |
|---|---|---|
| **flattrade** | ✅ fixed today | ✅ fixed today |
| **shoonya** | ❌ still `positions_response[0]["stat"]` — same `KeyError: 0` / `IndexError` | ❌ no list-type guard — same `AttributeError` |
| **zebu** | ❌ still `positions_response[0]["stat"]` — same defect | ❌ no list-type guard — same defect |
| **tradesmart** | ⚠️ guarded against a non-list, **but** a bare error dict falls through to the final `return {"status": "success", "message": "All Open Positions SquaredOff"}, 200` — a **false success** on an unreadable book | ⚠️ has the list guard, **missing** the per-item `isinstance(order, dict)` guard |

`shoonya` and `zebu` are Flattrade's sibling plugins (same Noren/`norenordno`
family, near-identical file layout), so they are the highest-probability next
failures. `tradesmart` is the more dangerous one of the three: it fails
*silently* — reporting success while placing no square-off orders. Because
`close_position_with_auth` maps any broker 200 to "All Open Positions Squared
Off", the caller cannot tell the difference.

---

## 9. Open follow-ups (not done, deliberately out of scope today)

1. **Port the two guards to `shoonya` and `zebu`** — same family, same
   production shape. Decision belongs to whoever owns those broker plugins;
   changing a broker adapter is a behaviour change and needs an explicit go-ahead.
2. **Fix `tradesmart`'s false success** — return a truthful "could not read the
   position book" answer instead of `"All Open Positions SquaredOff"`.
3. **Add pytest coverage** for these two functions across all four brokers, so
   the matrices in §3.6 / §4.6 are executed by CI and not only by a one-off
   harness. A single parametrised test over the response shapes would have
   caught both bugs.
4. **`kb/changelog.md` is still the starter template** — the two fixes are
   recorded in `kb/gotchas.md` but not in the changelog note.
5. **Check the other brokers' cancel-all loops** for the same
   iterate-before-type-check pattern (`fyers`, `upstox`, `pocketful` are
   suspect by inspection of their `order.get("status")` usage, but their reply
   shapes were **not** verified and are not claimed here).

---

## 10. Upgrade risk — read this before any OpenAlgo upgrade

`broker/flattrade/api/order_api.py` is **vendored upstream code**, not
Qwantonomous-owned. These two fixes are **local patches**. An OpenAlgo upgrade
that overwrites `broker/flattrade/api/order_api.py` will silently reintroduce
both defects — and the symptom will be a spurious "failed to square off" on a
flat account, which is easy to misdiagnose as a broker problem.

After **any** upgrade, re-check that both guards are still present:

- `close_all_positions` must not index `positions_response` before a
  falsy check and a dict/list normalisation.
- `cancel_all_orders_api` must contain `isinstance(order_book_response, list)`.

Both are recorded in `kb/gotchas.md` as `LOCAL PATCH (2026-10-22, commit …)`
entries for exactly this reason.

---

## 11. How to reproduce and re-verify

**Reproduce the original failures** (against a pre-fix copy): call square-off or
cancel-all on a Flattrade account whose PositionBook/OrderBook read returns a
bare `{"stat": "Not_Ok", "emsg": ...}`. Pre-fix, `log/errors.jsonl` gains a
`KeyError: 0` (square-off) or `AttributeError: 'str' object has no attribute
'get'` (cancel-all) entry.

**Re-verify the fix** without a live broker: extract
`close_all_positions` / `cancel_all_orders_api` from the live file with `ast`,
stub the helper functions, and feed the eight / six shapes in §3.6 and §4.6.
Every row must match the "After" column.

**Indirect check:** a flat-account square-off must produce **no new**
`services.close_position_service` error line in `log/errors.jsonl`, and the API
must answer 200 "All Open Positions Squared Off".

---

## Appendix A — raw tracebacks (verbatim from `log/errors.jsonl`)

```
=== services.close_position_service | Error in broker_module.close_all_positions: 0
Traceback (most recent call last):

  File "C:\opt\servers\qwantonomous\services\close_position_service.py", line 141, in close_position_with_auth
    response_code, status_code = broker_module.close_all_positions(api_key, auth_token)
                                 ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^

  File "C:\opt\servers\qwantonomous\broker\flattrade\api\order_api.py", line 278, in close_all_positions
    if positions_response is None or positions_response[0]["stat"] == "Not_Ok":
                                     ~~~~~~~~~~~~~~~~~~^^^

KeyError: 0

=== services.cancel_all_order_service | Error in broker_module.cancel_all_orders_api: 'str' object has no attribute 'get'
Traceback (most recent call last):

  File "C:\opt\servers\qwantonomous\services\cancel_all_order_service.py", line 131, in cancel_all_orders_with_auth
    canceled_orders, failed_cancellations = broker_module.cancel_all_orders_api(
                                            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^
        order_data, auth_token
        ^^^^^^^^^^^^^^^^^^^^^^
    )
    ^

  File "C:\opt\servers\qwantonomous\broker\flattrade\api\order_api.py", line 404, in cancel_all_orders_api
    if normalize_order_status(order.get("status")) == "open"
                              ^^^^^^^^^

AttributeError: 'str' object has no attribute 'get'
```

Line numbers in the traceback are the **pre-fix** positions; the current file
has the comments and guards described in §3.5 and §4.5.

## Appendix B — commit messages (verbatim subjects and bodies)

```
24161aa  fix(flattrade): flat position book crashed square-off into HTTP 500
4d662f5  fix(flattrade): failed OrderBook read crashed cancel-all into HTTP 500
7e84e36  docs(kb): record the local flattrade close_all_positions patch
05244b7  docs(kb): record the local cancel_all_orders_api patch
3e79860  Initial commit
```

## Appendix C — relevant code landmarks

| Thing | Where |
|---|---|
| Broker HTTP caller (returns raw JSON) | `broker/flattrade/api/order_api.py#get_api_response` |
| Position book read | `broker/flattrade/api/order_api.py#get_positions` |
| Order book read | `broker/flattrade/api/order_api.py#get_order_book` |
| Square-off (fixed) | `broker/flattrade/api/order_api.py#close_all_positions` |
| Cancel-all (fixed) | `broker/flattrade/api/order_api.py#cancel_all_orders_api` |
| Status normaliser | `broker/flattrade/mapping/order_data.py#normalize_order_status` |
| Square-off service (500 mapping) | `services/close_position_service.py#close_position_with_auth` |
| Cancel-all service (500 mapping) | `services/cancel_all_order_service.py#cancel_all_orders_with_auth` |
| KB record of both patches | `kb/gotchas.md` (two `LOCAL PATCH (2026-10-22 …)` entries) |
