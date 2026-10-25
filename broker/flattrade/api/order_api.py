import json
import os

import httpx
import threading
import time

from broker.flattrade.mapping.order_data import normalize_order_status
from broker.flattrade.mapping.transform_data import (
    MPPPriceUnavailable,
    map_product_type,
    reverse_map_product_type,
    transform_data,
    transform_modify_order_data,
)
from database.auth_db import get_auth_token
from database.token_db import get_br_symbol, get_symbol, get_token
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def get_api_response(endpoint, auth, method="GET", payload=""):
    AUTH_TOKEN = auth

    full_api_key = os.getenv("BROKER_API_KEY")
    api_key = full_api_key.split(":::")[0]

    data = f'{{"uid": "{api_key}", "actid": "{api_key}"}}'

    if endpoint == "/PiConnectAPI/Holdings":
        data = f'{{"uid": "{api_key}", "actid": "{api_key}", "prd": "C"}}'

    payload = "jData=" + data + "&jKey=" + AUTH_TOKEN

    # Get the shared httpx client
    client = get_httpx_client()

    if endpoint == "/PiConnectAPI/Holdings":
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    else:
        headers = {"Content-Type": "application/json"}

    url = f"https://piconnect.flattrade.in{endpoint}"
    response = client.request(method, url, content=payload, headers=headers)
    data = response.text

    return json.loads(data)


def get_order_book(auth):
    response = get_api_response("/PiConnectAPI/OrderBook", auth, method="POST")
    logger.debug(f"Flattrade OrderBook Response: {response}")
    return response


def get_trade_book(auth):
    response = get_api_response("/PiConnectAPI/TradeBook", auth, method="POST")
    logger.debug(f"Flattrade TradeBook Response: {response}")
    return response


def get_positions(auth):
    response = get_api_response("/PiConnectAPI/PositionBook", auth, method="POST")
    logger.debug(f"Flattrade PositionBook Response: {response}")
    return response


def get_holdings(auth):
    response = get_api_response("/PiConnectAPI/Holdings", auth, method="POST")
    logger.debug(f"Flattrade Holdings Response: {response}")
    return response


# --- Per-Symbol Smart Order Lock ---
# Ensures only one smart order per symbol executes at a time.
# Others queue and execute sequentially, each getting a fresh position book.
_symbol_locks = {}          # {symbol_key: threading.Lock}
_symbol_locks_lock = threading.Lock()

# --- Position Book Cache ---
# Caches get_positions() for 1 second. Invalidated after each smart order placement.
_position_cache = {}        # {auth_token: {"data": ..., "timestamp": ...}}
_position_cache_lock = threading.Lock()
_POSITION_CACHE_TTL = 1.0   # seconds


def _get_symbol_lock(symbol, exchange, product):
    """Get or create a per-symbol lock for serializing smart orders."""
    key = f"{symbol}:{exchange}:{product}"
    with _symbol_locks_lock:
        if key not in _symbol_locks:
            _symbol_locks[key] = threading.Lock()
        return _symbol_locks[key]


def _get_cached_positions(auth):
    """Get positions from cache if fresh, otherwise fetch from broker API."""
    with _position_cache_lock:
        now = time.monotonic()
        cached = _position_cache.get(auth)
        if cached and (now - cached["timestamp"]) < _POSITION_CACHE_TTL:
            return cached["data"]

    # Cache miss or expired - fetch from broker
    positions_data = get_positions(auth)

    with _position_cache_lock:
        _position_cache[auth] = {"data": positions_data, "timestamp": time.monotonic()}

    return positions_data


def _invalidate_position_cache(auth):
    """Invalidate the position cache so the next caller fetches fresh data."""
    with _position_cache_lock:
        _position_cache.pop(auth, None)


# --- Per-account square-off lock ---
# Serialises close_all_positions per auth token. Two sweeps MUST NOT read the
# same stale netqty: each would sell the whole long and the second one would
# open a SHORT - the unintended option-selling defect this guards against.
# Unlike _symbol_locks this is account-wide, because one sweep touches every
# position at once.
_close_locks = {}            # {auth_token: threading.Lock}
_close_locks_lock = threading.Lock()

# Grace period after cancelling a stale working SELL, before re-reading the
# books. Flattrade needs a moment to release the cancelled quantity (the same
# reason smarts orders invalidate the cache after placing).
_CLOSE_CANCEL_SETTLE = 0.15


def _get_close_lock(auth):
    """Get or create the per-account square-off lock."""
    key = auth or ""
    with _close_locks_lock:
        if key not in _close_locks:
            _close_locks[key] = threading.Lock()
        return _close_locks[key]


# --- Read-vs-empty distinction ---
# Noren answers a book request with a bare {"stat": "Not_Ok", "emsg": ...} dict
# when it has nothing to return. That shape means EMPTY only when emsg says so
# ("Error Occurred : 5 \"no data\""); every other Not_Ok - Session Expired, auth
# failure, a gateway error - is a genuine READ FAILURE. Collapsing the two into
# "0" is how a close or a smart order used to act on a book it could not see.
_EMPTY_BOOK_MARKERS = ("no data",)


class BrokerReadError(Exception):
    """A Flattrade book read genuinely failed (as opposed to being empty)."""


def _as_int(value):
    """Tolerant int for Noren's string numbers ("75", "75.0", "", None)."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _is_empty_book_response(response):
    """True only when a Not_Ok reply specifically means "there are no rows"."""
    if not isinstance(response, dict) or response.get("stat") != "Not_Ok":
        return False
    emsg = str(response.get("emsg") or response.get("message") or "").lower()
    return any(marker in emsg for marker in _EMPTY_BOOK_MARKERS)


def _read_book(response, what):
    """Normalise a Noren book reply to a list of rows, or raise BrokerReadError.

    [] is a genuinely EMPTY book; raising is the only way to say "I could not
    read it", so a caller can never mistake one for the other.
    """
    if response is None:
        raise BrokerReadError(f"{what} unavailable (no response from broker)")
    if isinstance(response, list):
        return [row for row in response if isinstance(row, dict)]
    if isinstance(response, dict):
        if _is_empty_book_response(response):
            return []
        if response.get("stat") == "Not_Ok":
            detail = response.get("emsg") or response.get("message") or "unknown error"
            raise BrokerReadError(f"{what} read failed ({detail})")
        # A single-row reply: some Noren endpoints answer with the row itself.
        return [response]
    raise BrokerReadError(
        f"{what} read failed (unexpected reply type {type(response).__name__})"
    )


def _position_key(position):
    """Join key between a position row and an order-book row (both RAW Noren)."""
    return (position.get("tsym"), position.get("exch"), position.get("prd"))


def _working_sell_orders(order_book_rows):
    """WORKING SELLs, keyed by (tsym, exch, prd) -> [{orderid, remaining}].

    Both books carry RAW Noren values here (tsym/exch/prd are not mapped), so
    the keys compare directly. `qty` minus `fillshares` is what is still
    working - the same filled/pending split the order-update adapter uses.

    The ORDER IDS matter: a SELL left working by an earlier sweep was priced
    off a premium that has since moved, so it may never fill. A later sweep
    must CANCEL it and re-price rather than skip the leg - skipping leaves the
    position open for ever and a retry loop can never converge.
    """
    working = {}
    for order in order_book_rows:
        if str(order.get("trantype") or "").strip().upper() != "S":
            continue
        if normalize_order_status(order.get("status")) != "open":
            continue
        remaining = _as_int(order.get("qty")) - _as_int(order.get("fillshares"))
        if remaining <= 0:
            continue
        key = (order.get("tsym"), order.get("exch"), order.get("prd"))
        working.setdefault(key, []).append(
            {"orderid": order.get("norenordno"), "remaining": remaining}
        )
    return working


def _cancel_orders(order_ids, auth):
    """Best-effort cancel; returns the ids the broker really cancelled."""
    cancelled = []
    for order_id in order_ids:
        if not order_id:
            continue
        try:
            response, status_code = cancel_order(order_id, auth)
            if status_code == 200:
                cancelled.append(order_id)
            else:
                logger.warning(
                    f"close_all_positions: could not cancel stale order "
                    f"{order_id} ({response})"
                )
        except Exception as error:
            logger.error(
                f"close_all_positions: cancel of stale order {order_id} "
                f"threw: {error}"
            )
    return cancelled


def get_open_position(tradingsymbol, exchange, producttype, auth):
    # Convert Trading Symbol from OpenAlgo Format to Broker Format Before Search in OpenPosition
    tradingsymbol = get_br_symbol(tradingsymbol, exchange)
    positions_data = _get_cached_positions(auth)

    logger.debug(f"{positions_data}")

    # An UNREADABLE book is NOT a flat one: BrokerReadError propagates so the
    # caller refuses to trade on a book it cannot actually see. Only a
    # genuinely EMPTY book reaches the summing loop below and yields "0".
    positions = _read_book(positions_data, "Flattrade position book")

    # Sum every matching row: a leg can appear more than once, and reading only
    # the first match under-reports the position.
    net_qty = 0
    for position in positions:
        if (
            position.get("tsym") == tradingsymbol
            and position.get("exch") == exchange
            and position.get("prd") == producttype
        ):
            net_qty += _as_int(position.get("netqty"))

    return str(net_qty)


def place_order_api(data, auth):
    AUTH_TOKEN = auth

    full_api_key = os.getenv("BROKER_API_KEY")
    BROKER_API_KEY = full_api_key.split(":::")[0]
    data["apikey"] = BROKER_API_KEY
    token = get_token(data["symbol"], data["exchange"])
    newdata = transform_data(data, token, AUTH_TOKEN)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    payload = "jData=" + json.dumps(newdata) + "&jKey=" + AUTH_TOKEN

    logger.debug(f"{payload}")
    # Get the shared httpx client
    client = get_httpx_client()

    url = "https://piconnect.flattrade.in/PiConnectAPI/PlaceOrder"
    res = client.post(url, content=payload, headers=headers)
    response_data = res.json()

    # Add status attribute for backward compatibility
    res.status = res.status_code

    if response_data["stat"] == "Ok":
        orderid = response_data["norenordno"]
    else:
        orderid = None
    return res, response_data, orderid


def place_smartorder_api(data, auth):
    AUTH_TOKEN = auth

    # If no API call is made in this function then res will return None
    res = None

    # Extract necessary info from data
    symbol = data.get("symbol")
    exchange = data.get("exchange")
    product = data.get("product")
    # Per-symbol lock: serialize smart orders per symbol
    symbol_lock = _get_symbol_lock(symbol, exchange, product)

    with symbol_lock:
        position_size = int(data.get("position_size", "0"))

        # Read a FRESH book: a book cached before the previous fill would make
        # this order's delta too large (it still shows the pre-fill quantity).
        _invalidate_position_cache(AUTH_TOKEN)

        # Get current open position for the symbol. Raises BrokerReadError on a
        # genuine read failure - never silently treated as flat.
        current_position = int(
            get_open_position(symbol, exchange, map_product_type(product), AUTH_TOKEN)
        )

        # A target on the FAR side of zero would FLIP the position (a long sold
        # past flat becomes a short). A "reduce to size" request never means
        # that, so clamp to flat unless the caller explicitly opts in with
        # allow_flip. This is the last line of defence against an unintended
        # option SELL, independent of whatever the caller computed.
        allow_flip = str(data.get("allow_flip") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        flipping = (current_position > 0 and position_size < 0) or (
            current_position < 0 and position_size > 0
        )
        if flipping and not allow_flip:
            logger.warning(
                f"place_smartorder_api: target position {position_size} would flip "
                f"the {current_position} position on {symbol}; clamping to flat "
                f"(pass allow_flip=true to override)"
            )
            position_size = 0

        logger.debug(f"position_size : {position_size}")
        logger.debug(f"Open Position : {current_position}")

        # Determine action based on position_size and current_position
        action = None
        quantity = 0

        # If both position_size and current_position are 0, do nothing
        if position_size == 0 and current_position == 0 and int(data["quantity"]) != 0:
            action = data["action"]
            quantity = data["quantity"]
            # logger.debug(f"action : {action}")
            # logger.debug(f"Quantity : {quantity}")
            res, response, orderid = place_order_api(data, AUTH_TOKEN)
            _invalidate_position_cache(AUTH_TOKEN)
            # logger.debug(f"{res}")
            # logger.debug(f"{response}")

            return res, response, orderid

        elif position_size == current_position:
            if int(data["quantity"]) == 0:
                response = {
                    "status": "success",
                    "message": "No OpenPosition Found. Not placing Exit order.",
                }
            else:
                response = {
                    "status": "success",
                    "message": "No action needed. Position size matches current position",
                }
            orderid = None
            return res, response, orderid  # res remains None as no API call was made

        if position_size == 0 and current_position > 0:
            action = "SELL"
            quantity = abs(current_position)
        elif position_size == 0 and current_position < 0:
            action = "BUY"
            quantity = abs(current_position)
        elif current_position == 0:
            action = "BUY" if position_size > 0 else "SELL"
            quantity = abs(position_size)
        else:
            if position_size > current_position:
                action = "BUY"
                quantity = position_size - current_position
                # logger.debug(f"smart buy quantity : {quantity}")
            elif position_size < current_position:
                action = "SELL"
                quantity = current_position - position_size
                # logger.debug(f"smart sell quantity : {quantity}")

        if action:
            # Prepare data for placing the order
            order_data = data.copy()
            order_data["action"] = action
            order_data["quantity"] = str(quantity)

            # logger.debug(f"{order_data}")
            # Place the order
            res, response, orderid = place_order_api(order_data, auth)
            _invalidate_position_cache(AUTH_TOKEN)
            # logger.debug(f"{res}")
            logger.debug(f"{response}")
            logger.debug(f"{orderid}")

            return res, response, orderid


def close_all_positions(current_api_key, auth):
    """Square off every open position, exactly once.

    Serialised per account and DE-DUPLICATED against orders already working at
    the broker. Without both guards a second sweep reads the same stale netqty,
    sells the same long again and takes the account through zero into a SHORT -
    the unintended option-selling defect this function now prevents.

    Returns (payload, status_code). A non-200 is a REAL failure (unreadable
    book, or a close order the broker rejected) and is propagated to the caller
    instead of being reported as a successful square-off.
    """
    AUTH_TOKEN = auth

    with _get_close_lock(AUTH_TOKEN):
        # Never decide from a book cached before a previous close landed.
        _invalidate_position_cache(AUTH_TOKEN)

        try:
            positions = _read_book(get_positions(AUTH_TOKEN), "Flattrade position book")
        except BrokerReadError as error:
            logger.error(f"close_all_positions: {error}")
            return {"status": "error", "message": str(error)}, 500
        except Exception as error:  # network / decode / broker 5xx
            logger.exception(f"close_all_positions: position book error: {error}")
            return {
                "status": "error",
                "message": f"Position book unavailable ({error})",
            }, 500

        # A genuinely empty book (or every row already flat) is a soft no-op.
        if not positions:
            return {"message": "No Open Positions Found"}, 200

        # A working SELL left behind by an EARLIER sweep was priced off a
        # premium that has since moved, so it may never fill. Skipping the leg
        # would leave the position open for ever and a retry loop could never
        # converge, so those orders are CANCELLED first and the close is then
        # re-priced against the current book.
        # Without a readable order book we cannot tell whether a close is already
        # working, so a sweep would risk overselling. Refuse instead.
        try:
            working_sells = _working_sell_orders(
                _read_book(get_order_book(AUTH_TOKEN), "Flattrade order book")
            )
        except BrokerReadError as error:
            logger.error(f"close_all_positions: refusing to close blind: {error}")
            return {"status": "error", "message": str(error)}, 500
        except Exception as error:
            logger.exception(f"close_all_positions: order book error: {error}")
            return {
                "status": "error",
                "message": f"Order book unavailable ({error})",
            }, 500

        # Cancel any working SELL that would block (or double-count against) the
        # close we are about to place for the SAME contract.
        stale_ids = []
        for position in positions:
            if _as_int(position.get("netqty")) <= 0:
                continue
            for order in working_sells.get(_position_key(position), []):
                stale_ids.append(order["orderid"])

        cancelled_ids = _cancel_orders(stale_ids, AUTH_TOKEN) if stale_ids else []
        if cancelled_ids:
            logger.warning(
                f"close_all_positions: cancelled {len(cancelled_ids)} stale working "
                f"SELL(s) {cancelled_ids} so the close can be re-priced"
            )
            # Let the broker release the cancelled quantity, then decide from a
            # FRESH position book + order book - never from the pre-cancel view.
            time.sleep(_CLOSE_CANCEL_SETTLE)
            _invalidate_position_cache(AUTH_TOKEN)
            try:
                positions = _read_book(
                    get_positions(AUTH_TOKEN), "Flattrade position book"
                )
                working_sells = _working_sell_orders(
                    _read_book(get_order_book(AUTH_TOKEN), "Flattrade order book")
                )
            except BrokerReadError as error:
                logger.error(
                    f"close_all_positions: re-read after cancel failed: {error}"
                )
                return {"status": "error", "message": str(error)}, 500

        closed = []
        covered = []
        failures = []

        # Loop through each position to close
        for position in positions:
            # Skip if net quantity is zero
            net_qty = _as_int(position.get("netqty"))
            if net_qty == 0:
                continue

            # get openalgo symbol to send to placeorder function
            symbol = get_symbol(position.get("token"), position.get("exch"))
            if not symbol:
                logger.error(
                    f"close_all_positions: no OpenAlgo symbol for token "
                    f"{position.get('token')} on {position.get('exch')}"
                )
                failures.append(
                    {
                        "symbol": position.get("tsym"),
                        "message": "symbol lookup failed",
                    }
                )
                continue
            logger.debug(f"The Symbol is {symbol}")

            # Determine action based on net quantity
            action = "SELL" if net_qty > 0 else "BUY"
            quantity = abs(net_qty)

            if net_qty > 0:
                # A long: subtract whatever a working SELL the broker would NOT
                # let us cancel still covers, so this sweep can never take the
                # position through zero. pop() consumes the key so a second row
                # for the same contract cannot double-count the same order.
                key = _position_key(position)
                still_working = sum(
                    order["remaining"]
                    for order in working_sells.pop(key, [])
                )
                if still_working:
                    quantity -= still_working
                    if quantity <= 0:
                        logger.info(
                            f"close_all_positions: {symbol} still covered by a "
                            f"working SELL for {still_working} that could not be "
                            f"cancelled - not selling again (netqty {net_qty})"
                        )
                        covered.append(
                            {"symbol": symbol, "working_sell_qty": still_working}
                        )
                        continue

            # Prepare the order payload
            place_order_payload = {
                "apikey": current_api_key,
                "strategy": "Squareoff",
                "symbol": symbol,
                "action": action,
                "exchange": position.get("exch"),
                "pricetype": "MARKET",
                "product": reverse_map_product_type(position.get("prd")),
                "quantity": str(quantity),
            }

            logger.debug(f"{place_order_payload}")

            # Place the order to close the position and HONOUR the result: a
            # rejected close must surface as a failure, not as a silent "success".
            try:
                res, response, orderid = place_order_api(place_order_payload, auth)
            except Exception as error:
                logger.exception(
                    f"close_all_positions: {symbol} close threw: {error}"
                )
                failures.append({"symbol": symbol, "message": str(error)})
                continue

            stat = response.get("stat") if isinstance(response, dict) else None
            http_status = getattr(res, "status", None)
            if http_status == 200 and stat == "Ok":
                closed.append(
                    {
                        "symbol": symbol,
                        "action": action,
                        "quantity": quantity,
                        "orderid": orderid,
                    }
                )
            else:
                detail = (
                    (response.get("emsg") or response.get("message"))
                    if isinstance(response, dict)
                    else None
                )
                logger.error(
                    f"close_all_positions: {symbol} close REJECTED "
                    f"(http={http_status} stat={stat} {detail})"
                )
                failures.append(
                    {
                        "symbol": symbol,
                        "message": detail or "broker rejected the close order",
                        "http_status": http_status,
                        "stat": stat,
                    }
                )

        # The book changed - never let a stale snapshot feed the next caller.
        _invalidate_position_cache(AUTH_TOKEN)

        if failures:
            logger.error(
                f"close_all_positions: {len(failures)} close order(s) failed: {failures}"
            )
            return {
                "status": "error",
                "message": f"{len(failures)} close order(s) failed: "
                + "; ".join(
                    f"{f.get('symbol')}: {f.get('message')}" for f in failures
                ),
                "closed": closed,
                "failed": failures,
            }, 500

        if closed:
            return {
                "status": "success",
                "message": "All Open Positions SquaredOff",
                "closed": closed,
            }, 200

        if covered:
            # Every open long is already covered by a working SELL: nothing was
            # sent, and nothing more may be sent without overselling.
            return {
                "status": "success",
                "message": "Close already working for every open position",
                "covered": covered,
            }, 200

        return {"message": "No Open Positions Found"}, 200


def cancel_order(orderid, auth):
    # Assuming you have a function to get the authentication token
    AUTH_TOKEN = auth
    full_api_key = os.getenv("BROKER_API_KEY")
    api_key = full_api_key.split(":::")[0]
    data = {"uid": api_key, "norenordno": orderid}

    payload = "jData=" + json.dumps(data) + "&jKey=" + AUTH_TOKEN
    # Set up the request headers
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    # Get the shared httpx client and send the request
    client = get_httpx_client()

    url = "https://piconnect.flattrade.in/PiConnectAPI/CancelOrder"
    res = client.post(url, content=payload, headers=headers)
    data = res.json()
    logger.debug(f"{data}")

    # Check if the request was successful
    if data.get("stat") == "Ok":
        # Return a success response
        return {"status": "success", "orderid": orderid}, 200
    else:
        # Return an error response
        return {
            "status": "error",
            "message": data.get("message", "Failed to cancel order"),
        }, res.status_code


def modify_order(data, auth):
    # Assuming you have a function to get the authentication token
    AUTH_TOKEN = auth
    full_api_key = os.getenv("BROKER_API_KEY")
    api_key = full_api_key.split(":::")[0]

    # Keep data["symbol"] in OpenAlgo format: the MPP conversion inside
    # transform_modify_order_data needs it for the quote, slab and tick-size
    # lookups. The transform derives the broker symbol itself via get_br_symbol.
    token = get_token(data["symbol"], data["exchange"])
    data["apikey"] = api_key

    try:
        transformed_data = transform_modify_order_data(data, token, AUTH_TOKEN)
    except (MPPPriceUnavailable, ValueError) as error:
        logger.error(f"Modify order NOT sent for {data.get('symbol')}: {error}")
        return {"status": "error", "message": str(error)}, 400
    # Set up the request headers
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = "jData=" + json.dumps(transformed_data) + "&jKey=" + AUTH_TOKEN

    logger.debug(f"Modify Order Payload: {payload}")
    logger.debug(f"Modify Order Data: {transformed_data}")

    # Get the shared httpx client
    client = get_httpx_client()

    url = "https://piconnect.flattrade.in/PiConnectAPI/ModifyOrder"
    res = client.post(url, content=payload, headers=headers)
    response = res.json()

    logger.debug(f"Modify Order Response: {response}")
    logger.debug(f"Modify Order Status Code: {res.status_code}")

    if response.get("stat") == "Ok":
        return {"status": "success", "orderid": data["orderid"]}, 200
    else:
        return {
            "status": "error",
            "message": response.get("emsg", "Failed to modify order"),
        }, res.status_code


def cancel_all_orders_api(data, auth):
    # Get the order book

    AUTH_TOKEN = auth

    order_book_response = get_order_book(AUTH_TOKEN)
    # logger.debug(f"{order_book_response}")
    if order_book_response is None:
        return [], []  # Return empty lists indicating failure to retrieve the order book

    # A FAILED OrderBook read does not come back as a list: it is a bare
    # {"stat": "Not_Ok", "emsg": ...} dict. Iterating that yields its string
    # KEYS, so `order.get("status")` raised AttributeError: 'str' object has no
    # attribute 'get' — OpenAlgo's cancel service turns that into HTTP 500
    # "Failed to cancel all orders due to internal error" even though there was
    # nothing pending to cancel. Same shape fix as close_all_positions above.
    if not isinstance(order_book_response, list):
        return [], []

    # Filter orders still working at the exchange (OPEN / PENDING / TRIGGER_PENDING)
    orders_to_cancel = [
        order
        for order in order_book_response
        if isinstance(order, dict)
        and normalize_order_status(order.get("status")) == "open"
    ]
    # logger.debug(f"{orders_to_cancel}")
    canceled_orders = []
    failed_cancellations = []

    # Cancel the filtered orders
    for order in orders_to_cancel:
        orderid = order["norenordno"]
        cancel_response, status_code = cancel_order(orderid, auth)
        if status_code == 200:
            canceled_orders.append(orderid)
        else:
            failed_cancellations.append(orderid)

    return canceled_orders, failed_cancellations
