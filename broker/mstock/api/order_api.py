import json
import os

import httpx
import threading
import time

from broker.mstock.mapping.transform_data import (
    get_mstock_symbol,
    map_product_type,
    reverse_map_product_type,
    transform_data,
    transform_modify_order_data,
)
from broker.mstock.api.rate_limiter import apply_order_rate_limit
from broker.mstock.mapping.order_data import map_broker_exchange_to_openalgo
from database.auth_db import get_auth_token
from database.token_db import get_symbol, get_token
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

# mStock Type B answers success as the string "True" (mock order book in the
# vendor SDK), as the lowercase "true" in older SDKs, and as a Python True.
_TRUE_STATUSES = (True, "true", "True")

# Order states in which the broker may still fill the order. Anything else
# (complete / traded / rejected / cancelled) cannot consume quantity later.
_WORKING_ORDER_STATES = {
    "open",
    "o-pending",
    "o-modified",
    "pending",
    "modified",
    "trigger pending",
    "transit",
    "partially filled",
}

# mStock signals "this book is genuinely empty" in the message rather than by
# an empty payload shape.
_EMPTY_BOOK_MARKERS = ("no data", "no record", "no position")


class BrokerReadError(Exception):
    """An mStock book read genuinely failed (as opposed to being empty)."""


def _as_int(value):
    """Tolerant int for mStock's string numbers ("390", "390.0", "", None)."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _status_ok(response):
    """True when an mStock Type B reply reports success."""
    return isinstance(response, dict) and _is_success(response.get("status"))


def _read_book(response, what):
    """Normalise an mStock book reply to a list of rows, or raise BrokerReadError.

    [] is a genuinely EMPTY book; raising is the only way to say "I could not
    read it", so a caller can never mistake one for the other. mStock answers
    a failure with {"status": "false", "message": ..., "data": null} and
    get_api_response returns {} when the payload cannot be decoded - both are
    READ FAILURES, not a flat account.
    """
    if response is None:
        raise BrokerReadError(f"{what} unavailable (no response from broker)")
    if isinstance(response, list):
        return [row for row in response if isinstance(row, dict)]
    if not isinstance(response, dict):
        raise BrokerReadError(
            f"{what} read failed (unexpected reply type {type(response).__name__})"
        )

    if _status_ok(response):
        data = response.get("data")
        if data is None:
            return []
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            # A single-row reply: some mStock endpoints answer with the row itself.
            return [data]
        raise BrokerReadError(
            f"{what} read failed (unexpected data type {type(data).__name__})"
        )

    message = str(response.get("message") or "").lower()
    if any(marker in message for marker in _EMPTY_BOOK_MARKERS):
        return []
    detail = response.get("message") or response.get("errorcode") or "unknown error"
    raise BrokerReadError(f"{what} read failed ({detail})")


def _is_success(status):
    """True for every shape mStock uses to say yes (True / "true" / "True" / "success").

    The vendor mock order book answers "True" while the placement docs answer
    "true". Accepting only one spelling silently turned a SUCCESSFUL order into
    an orderid-less reply, which a square-off would then read as a rejection.
    """
    if status in _TRUE_STATUSES:
        return True
    return isinstance(status, str) and status.strip().lower() == "success"


def _is_working_order(*statuses):
    """True when any of the supplied row statuses means the order is still live."""
    for status in statuses:
        if isinstance(status, str) and status.strip().lower() in _WORKING_ORDER_STATES:
            return True
    return False


def _position_key(row):
    """Join key between a position row and an order-book row (both RAW mStock)."""
    return (
        str(row.get("symboltoken") or "").strip(),
        str(row.get("exchange") or "").strip().upper(),
        str(row.get("producttype") or "").strip().upper(),
    )


def _row_matches_exchange(row, exchange):
    """Does a position row belong to the requested OpenAlgo exchange?"""
    raw = str(row.get("exchange") or "").strip().upper()
    wanted = str(exchange or "").strip().upper()
    if raw == wanted:
        return True
    # mStock reports a derivative under its CASH exchange (NSE/BSE) and names
    # the segment separately, while OpenAlgo asks for NFO/BFO. Without this an
    # NFO contract reads as FLAT, and a smart order would then sell a live long.
    return (
        map_broker_exchange_to_openalgo(raw, row.get("instrumenttype", "") or "").upper()
        == wanted
    )


def _row_matches_product(row, producttype):
    """Does a position row belong to the requested (broker-mapped) product?"""
    wanted = str(producttype or "").strip().upper()
    if not wanted:
        return True
    raw = str(row.get("producttype") or "").strip().upper()
    return raw == wanted or str(reverse_map_product_type(raw) or "").upper() == wanted


# --- Per-account square-off lock ---
# Serialises close_all_positions per auth token. Two sweeps MUST NOT read the
# same stale netqty: each would sell the whole long and the second one would
# open a SHORT - the unintended option-selling defect this guards against.
# Unlike _symbol_locks this is account-wide, because one sweep touches every
# position at once.
_close_locks = {}            # {auth_token: threading.Lock}
_close_locks_lock = threading.Lock()

# Grace period after cancelling a stale working SELL, before re-reading the
# books. The broker needs a moment to release the cancelled quantity (the same
# reason a smart order invalidates the cache after placing).
_CLOSE_CANCEL_SETTLE = 0.15


def _get_close_lock(auth):
    """Get or create the per-account square-off lock."""
    key = auth or ""
    with _close_locks_lock:
        if key not in _close_locks:
            _close_locks[key] = threading.Lock()
        return _close_locks[key]


def _working_sell_orders(order_book_rows):
    """WORKING SELLs, keyed by (symboltoken, exchange, producttype).

    `quantity` minus `filledshares` is what is still working at the broker.

    The ORDER IDS matter: a SELL left working by an earlier sweep was priced
    off a level that has since moved, so it may never fill. A later sweep must
    CANCEL it and re-price rather than skip the leg - skipping leaves the
    position open for ever and a retry loop can never converge.
    """
    working = {}
    for order in order_book_rows:
        if str(order.get("transactiontype") or "").strip().upper() != "SELL":
            continue
        if not _is_working_order(order.get("status"), order.get("orderstatus")):
            continue
        remaining = _as_int(order.get("quantity")) - _as_int(order.get("filledshares"))
        if remaining <= 0:
            continue
        working.setdefault(_position_key(order), []).append(
            {"orderid": order.get("orderid"), "remaining": remaining}
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


def get_api_response(endpoint, auth, method="GET", payload=""):
    """
    Generic API request handler for mStock Type B APIs.

    Args:
        endpoint: API endpoint path
        auth: Authentication token
        method: HTTP method (GET, POST, PUT, DELETE)
        payload: Request payload

    Returns:
        dict: JSON response from API
    """
    auth_token = auth
    api_key = os.getenv("BROKER_API_SECRET")

    client = get_httpx_client()

    headers = {
        "X-Mirae-Version": "1",
        "Authorization": f"Bearer {auth_token}",
        "X-PrivateKey": api_key,
        "Content-Type": "application/json",
    }

    url = f"https://api.mstock.trade/openapi/typeb{endpoint}"

    if method == "GET":
        response = client.get(url, headers=headers)
    elif method == "POST":
        response = client.post(url, headers=headers, content=payload)
    else:
        response = client.request(method, url, headers=headers, content=payload)

    # Add status attribute for compatibility with existing codebase
    response.status = response.status_code

    # Handle empty response
    if not response.text:
        return {}

    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse JSON response from {endpoint}: {response.text}")
        return {}


def get_order_book(auth):
    """Fetch the order book from mStock Type B API."""
    return get_api_response("/orders", auth)


def get_trade_book(auth):
    """Fetch the trade book from mStock Type B API."""
    return get_api_response("/tradebook", auth)


def get_positions(auth):
    """Fetch positions from mStock Type B API."""
    return get_api_response("/portfolio/positions", auth)


def get_holdings(auth):
    """Fetch holdings from mStock Type B API."""
    return get_api_response("/portfolio/holdings", auth)


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
    """Invalidate the position cache so the next queued order fetches fresh data."""
    with _position_cache_lock:
        _position_cache.pop(auth, None)



def get_open_position(tradingsymbol, exchange, producttype, auth):
    """Net quantity for one contract, SUMMED over every matching position row.

    Args:
        tradingsymbol: OpenAlgo format symbol
        exchange: Exchange name
        producttype: Product type (mapped to broker format)
        auth: Authentication token

    Returns:
        str: Net quantity as string

    An UNREADABLE book is NOT a flat one: BrokerReadError propagates so the
    caller refuses to trade on a book it cannot actually see. Only a genuinely
    EMPTY book reaches the summing loop and yields "0". Reading only the first
    match - and there can be several rows for one contract - under-reports the
    position, which is how a close sells too little or a smart order sells too
    much.
    """
    # Get symboltoken for the tradingsymbol
    token = get_token(tradingsymbol, exchange)
    if not token:
        logger.warning(f"Token not found for {tradingsymbol} on {exchange}")
        return "0"

    positions = _read_book(_get_cached_positions(auth), "mStock position book")

    logger.debug(
        f"Looking for position: symboltoken={token}, exchange={exchange}, "
        f"producttype={producttype}"
    )

    wanted_token = str(token).strip()
    net_qty = 0
    for position in positions:
        # Match using symboltoken instead of tradingsymbol (which is empty in mStock API)
        if str(position.get("symboltoken") or "").strip() != wanted_token:
            continue
        if not _row_matches_exchange(position, exchange):
            continue
        if not _row_matches_product(position, producttype):
            continue
        net_qty += _as_int(position.get("netqty"))

    return str(net_qty)


def place_order_api(data, auth):
    """
    Place a regular order on mStock Type B API.

    Args:
        data: OpenAlgo order data
        auth: Authentication token

    Returns:
        tuple: (response, response_data, orderid)
    """
    auth_token = auth
    api_key = os.getenv("BROKER_API_SECRET")

    # Get token and transform data
    token = get_token(data["symbol"], data["exchange"])
    transformed_data = transform_data(data, token)

    headers = {
        "X-Mirae-Version": "1",
        "Authorization": f"Bearer {auth_token}",
        "X-PrivateKey": api_key,
        "Content-Type": "application/json",
    }

    payload = json.dumps(transformed_data)
    logger.debug(f"Place order payload: {payload}")

    client = get_httpx_client()

    # Pace against the order-API plan BEFORE sending. A burst that trips the
    # plan's ceiling is answered with status "false", which place_order_api
    # returns as an orderid-less reply - and a square-off correctly reports that
    # as a rejected close. Pacing is what keeps an exit sweep from rejecting
    # its own orders.
    apply_order_rate_limit()

    response = client.post(
        "https://api.mstock.trade/openapi/typeb/orders/regular", headers=headers, content=payload
    )

    # Add status attribute for compatibility
    response.status = response.status_code

    # Parse JSON response
    response_data = response.json()

    logger.debug(f"Place order response status code: {response.status_code}")
    logger.debug(f"Place order response data type: {type(response_data)}")
    logger.debug(f"Place order response data: {response_data}")

    # Handle both dict and list responses
    orderid = None

    # mStock Type B API returns a list with single dict element
    if isinstance(response_data, list) and len(response_data) > 0:
        logger.debug("API returned list, extracting first element")
        response_dict = response_data[0]

        # Extract orderid from the dict
        if _is_success(response_dict.get("status")) and response_dict.get("data"):
            orderid = response_dict["data"].get("orderid")
            logger.debug(f"Extracted orderid: {orderid}")

        # Keep the dict format for response_data for compatibility
        response_data = response_dict

    elif isinstance(response_data, dict):
        # Standard dict response format
        if _is_success(response_data.get("status")) and response_data.get("data"):
            orderid = response_data["data"].get("orderid")
            logger.debug(f"Extracted orderid: {orderid}")

    return response, response_data, orderid


def place_smartorder_api(data, auth):
    """
    Place a smart order that adjusts based on current position.

    Args:
        data: OpenAlgo order data with position_size
        auth: Authentication token

    Returns:
        tuple: (response, response_data, orderid)
    """
    auth_token = auth
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
        _invalidate_position_cache(auth_token)

        # Get current open position for the symbol. Raises BrokerReadError on a
        # genuine read failure - never silently treated as flat.
        current_position = int(
            get_open_position(symbol, exchange, map_product_type(product), auth_token)
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

        logger.debug(f"position_size: {position_size}")
        logger.debug(f"Open Position: {current_position}")

        action = None
        quantity = 0

        # If both position_size and current_position are 0, do nothing
        if position_size == 0 and current_position == 0 and int(data["quantity"]) != 0:
            action = data["action"]
            quantity = data["quantity"]
            res, response, orderid = place_order_api(data, auth_token)
            _invalidate_position_cache(auth_token)
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
            return res, response, orderid

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
            elif position_size < current_position:
                action = "SELL"
                quantity = current_position - position_size

        if action:
            # Prepare data for placing the order
            order_data = data.copy()
            order_data["action"] = action
            order_data["quantity"] = str(quantity)

            # Place the order
            res, response, orderid = place_order_api(order_data, auth)
            _invalidate_position_cache(auth_token)
            logger.debug(f"Smart order response: {response}")
            logger.debug(f"Smart order ID: {orderid}")

            return res, response, orderid


def close_all_positions(current_api_key, auth):
    """Square off every open position, exactly once, honestly.

    Serialised per account and DE-DUPLICATED against orders already working at
    the broker. Without both guards a second sweep reads the same stale netqty,
    sells the same long again and takes the account through zero into a SHORT -
    the unintended option-selling defect this function now prevents.

    Returns (payload, status_code). A non-200 is a REAL failure (unreadable
    book, or a close order the broker rejected) and is propagated to the caller
    instead of being reported as a successful square-off.
    """
    auth_token = auth

    with _get_close_lock(auth_token):
        # Never decide from a book cached before a previous close landed.
        _invalidate_position_cache(auth_token)

        try:
            positions = _read_book(get_positions(auth_token), "mStock position book")
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

        # A working SELL left behind by an EARLIER sweep was priced off a level
        # that has since moved, so it may never fill. Skipping the leg would
        # leave the position open for ever and a retry loop could never
        # converge, so those orders are CANCELLED first and the close is then
        # re-priced against the current book.
        # Without a readable order book we cannot tell whether a close is
        # already working, so a sweep would risk overselling. Refuse instead.
        try:
            working_sells = _working_sell_orders(
                _read_book(get_order_book(auth_token), "mStock order book")
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

        cancelled_ids = _cancel_orders(stale_ids, auth_token) if stale_ids else []
        if cancelled_ids:
            logger.warning(
                f"close_all_positions: cancelled {len(cancelled_ids)} stale working "
                f"SELL(s) {cancelled_ids} so the close can be re-priced"
            )
            # Let the broker release the cancelled quantity, then decide from a
            # FRESH position book + order book - never from the pre-cancel view.
            time.sleep(_CLOSE_CANCEL_SETTLE)
            _invalidate_position_cache(auth_token)
            try:
                positions = _read_book(
                    get_positions(auth_token), "mStock position book"
                )
                working_sells = _working_sell_orders(
                    _read_book(get_order_book(auth_token), "mStock order book")
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
            netqty = _as_int(position.get("netqty"))
            if netqty == 0:
                continue

            # Determine action based on net quantity
            action = "SELL" if netqty > 0 else "BUY"
            quantity = abs(netqty)

            # Determine the correct exchange for symbol lookup: mStock reports a
            # derivative under its CASH exchange and names the segment
            # separately, while OpenAlgo uses NFO/BFO.
            lookup_exchange = map_broker_exchange_to_openalgo(
                position.get("exchange") or "",
                position.get("instrumenttype", "") or "",
            )

            # Get OpenAlgo symbol to send to placeorder function
            symbol = get_symbol(position.get("symboltoken"), lookup_exchange)

            # Skip if symbol not found - but SAY SO: a silent skip leaves the
            # position open while the caller is told everything was squared off.
            if not symbol:
                logger.error(
                    f"close_all_positions: no OpenAlgo symbol for token "
                    f"{position.get('symboltoken')} on {lookup_exchange}"
                )
                failures.append(
                    {
                        "symbol": position.get("symbolname")
                        or position.get("symboltoken"),
                        "message": "symbol lookup failed",
                    }
                )
                continue

            if netqty > 0:
                # A long: subtract whatever a working SELL the broker would NOT
                # let us cancel still covers, so this sweep can never take the
                # position through zero. pop() consumes the key so a second row
                # for the same contract cannot double-count the same order.
                still_working = sum(
                    order["remaining"]
                    for order in working_sells.pop(_position_key(position), [])
                )
                if still_working:
                    quantity -= still_working
                    if quantity <= 0:
                        logger.info(
                            f"close_all_positions: {symbol} still covered by a "
                            f"working SELL for {still_working} that could not be "
                            f"cancelled - not selling again (netqty {netqty})"
                        )
                        covered.append(
                            {"symbol": symbol, "working_sell_qty": still_working}
                        )
                        continue

            logger.debug(
                f"Closing position for symbol: {symbol}, quantity: {quantity}, action: {action}"
            )

            # Prepare the order payload
            # Use lookup_exchange (NFO/BFO for derivatives) instead of position exchange (NSE/BSE)
            place_order_payload = {
                "apikey": current_api_key,
                "strategy": "Squareoff",
                "symbol": symbol,
                "action": action,
                "exchange": lookup_exchange,  # Use NFO/BFO for derivatives, not NSE/BSE
                "pricetype": "MARKET",
                "product": reverse_map_product_type(position.get("producttype")),
                "quantity": str(quantity),
            }

            logger.debug(f"Square off payload: {place_order_payload}")

            # Place the order to close the position and HONOUR the result: a
            # rejected close must surface as a failure, not a silent "success".
            try:
                res, response, orderid = place_order_api(place_order_payload, auth)
            except Exception as error:
                logger.exception(
                    f"close_all_positions: {symbol} close threw: {error}"
                )
                failures.append({"symbol": symbol, "message": str(error)})
                continue

            http_status = getattr(res, "status", None)
            if http_status == 200 and orderid and _status_ok(response):
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
                    response.get("message") or response.get("errorcode")
                    if isinstance(response, dict)
                    else None
                )
                logger.error(
                    f"close_all_positions: {symbol} close REJECTED "
                    f"(http={http_status} orderid={orderid} {detail})"
                )
                failures.append(
                    {
                        "symbol": symbol,
                        "message": detail or "broker rejected the close order",
                        "http_status": http_status,
                        "orderid": orderid,
                    }
                )

        # The book changed - never let a stale snapshot feed the next caller.
        _invalidate_position_cache(auth_token)

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
    """
    Cancel a pending order on mStock Type B API.

    Args:
        orderid: Order ID to cancel
        auth: Authentication token

    Returns:
        tuple: (response_dict, status_code)
    """
    auth_token = auth
    api_key = os.getenv("BROKER_API_SECRET")

    client = get_httpx_client()

    headers = {
        "X-Mirae-Version": "1",
        "Authorization": f"Bearer {auth_token}",
        "X-PrivateKey": api_key,
        "Content-Type": "application/json",
    }

    # Prepare payload for Type B (variety and orderid in body)
    payload_data = {"variety": "NORMAL", "orderid": orderid}

    logger.debug(f"Cancelling order {orderid}")
    logger.debug(f"Cancel order payload: {json.dumps(payload_data)}")

    # DELETE request with orderid in both URL path and body
    # Using json parameter instead of content/data for httpx compatibility
    # Cancels share the same order-API budget as placements.
    apply_order_rate_limit()
    response = client.request(
        method="DELETE",
        url=f"https://api.mstock.trade/openapi/typeb/orders/regular/{orderid}",
        headers=headers,
        json=payload_data,
    )

    # Add status attribute for compatibility
    response.status = response.status_code

    logger.debug(f"Cancel order response status code: {response.status_code}")
    logger.debug(f"Cancel order response: {response.text}")

    try:
        data = json.loads(response.text)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse cancel order response: {response.text}")
        return {"status": "error", "message": "Invalid response from broker"}, 500

    # Handle list response (like place order)
    if isinstance(data, list) and len(data) > 0:
        logger.debug("API returned list, extracting first element")
        data = data[0]

    # Check if the request was successful
    if _is_success(data.get("status")) or data.get("message") == "SUCCESS":
        logger.debug(f"Order {orderid} cancelled successfully")
        return {"status": "success", "orderid": orderid}, 200
    else:
        error_message = data.get("message", "Failed to cancel order")
        logger.error(f"Failed to cancel order {orderid}: {error_message}")
        return {"status": "error", "message": error_message}, response.status


def modify_order(data, auth):
    """
    Modify an existing order on mStock Type B API.

    Args:
        data: OpenAlgo modify order data with fields:
            - orderid: Order ID to modify
            - symbol: OpenAlgo symbol
            - exchange: Exchange code
            - action: BUY/SELL
            - quantity: Order quantity
            - price: Order price
            - trigger_price: Trigger price (for SL orders)
            - pricetype: MARKET/LIMIT/SL/SL-M
            - product: CNC/MIS/NRML
        auth: Authentication token

    Returns:
        tuple: (response_dict, status_code)
    """
    auth_token = auth
    api_key = os.getenv("BROKER_API_SECRET")

    client = get_httpx_client()

    # Get token for the symbol
    try:
        token = get_token(data["symbol"], data["exchange"])
        if not token:
            logger.error(
                f"Token not found for symbol {data['symbol']}, exchange {data['exchange']}"
            )
            return {"status": "error", "message": "Symbol token not found in database"}, 400
    except Exception as e:
        logger.error(f"Error getting token: {e}")
        return {"status": "error", "message": f"Failed to get symbol token: {str(e)}"}, 400

    # Transform data to mStock Type B format
    transformed_data = transform_modify_order_data(data, token)

    headers = {
        "X-Mirae-Version": "1",
        "Authorization": f"Bearer {auth_token}",
        "X-PrivateKey": api_key,
        "Content-Type": "application/json",
    }

    orderid = data["orderid"]

    logger.debug(f"Modifying order {orderid} for symbol {data['symbol']}")
    logger.debug(f"Modify order payload: {json.dumps(transformed_data)}")

    # PUT request with orderid in URL path
    # Using json parameter for httpx compatibility
    # Modifies share the same order-API budget as placements.
    apply_order_rate_limit()
    response = client.request(
        method="PUT",
        url=f"https://api.mstock.trade/openapi/typeb/orders/regular/{orderid}",
        headers=headers,
        json=transformed_data,
    )

    # Add status attribute for compatibility
    response.status = response.status_code

    logger.debug(f"Modify order response status code: {response.status_code}")
    logger.debug(f"Modify order response: {response.text}")

    try:
        response_data = json.loads(response.text)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse modify order response: {response.text}")
        return {"status": "error", "message": "Invalid response from broker"}, 500

    # Handle list response (like place order/cancel order)
    if isinstance(response_data, list) and len(response_data) > 0:
        logger.debug("API returned list, extracting first element")
        response_data = response_data[0]

    # Check if the request was successful
    # mStock Type B returns status as boolean True or string "true"
    if _is_success(response_data.get("status")) or response_data.get("message") == "SUCCESS":
        # Extract orderid from response data
        if response_data.get("data") and isinstance(response_data["data"], dict):
            modified_orderid = response_data["data"].get("orderid", orderid).strip()
        else:
            modified_orderid = orderid

        logger.debug(f"Order {orderid} modified successfully to {modified_orderid}")
        return {"status": "success", "orderid": modified_orderid}, 200
    else:
        error_message = response_data.get("message", "Failed to modify order")
        errorcode = response_data.get("errorcode", "")
        logger.error(f"Failed to modify order {orderid}: {error_message} (errorcode: {errorcode})")
        return {"status": "error", "message": error_message}, response.status


def cancel_all_orders_api(data, auth):
    """
    Cancel all pending orders using mStock Type B cancelall endpoint.

    Args:
        data: Request data (not used for mStock Type B)
        auth: Authentication token

    Returns:
        tuple: (canceled_orders_list, failed_cancellations_list)
    """
    auth_token = auth
    api_key = os.getenv("BROKER_API_SECRET")

    # First, get the list of pending orders to return their IDs
    logger.debug("Fetching order book to identify pending orders")

    # A FAILED order-book read is NOT an empty one: mStock answers a failure with
    # {"status": "false", "message": ..., "data": null} and get_api_response
    # returns {} when the payload cannot be decoded. Calling .get() on a bare
    # failure dict used to look like "nothing pending", and an untyped
    # `.get("data")` on a list reply crashed cancel-all into HTTP 500
    # "Failed to cancel all orders due to internal error" with nothing pending.
    try:
        order_book_rows = _read_book(get_order_book(auth_token), "mStock order book")
    except BrokerReadError as error:
        logger.error(f"cancel_all_orders_api: {error}")
        return [], []

    # Filter orders that are still working at the broker
    pending_orders = [
        order
        for order in order_book_rows
        if _is_working_order(order.get("status"), order.get("orderstatus"))
    ]
    pending_order_ids = [
        order.get("orderid") for order in pending_orders if order.get("orderid")
    ]
    logger.debug(
        f"Found {len(pending_order_ids)} pending orders to cancel: {pending_order_ids}"
    )

    # If no pending orders, return early
    if not pending_order_ids:
        logger.debug("No pending orders to cancel")
        return [], []

    # Now call the cancelall endpoint
    client = get_httpx_client()

    headers = {
        "X-Mirae-Version": "1",
        "Authorization": f"Bearer {auth_token}",
        "X-PrivateKey": api_key,
        "Content-Type": "application/json",
    }

    logger.debug("Calling mStock Type B cancelall endpoint")

    # POST request to cancel all orders at once
    apply_order_rate_limit()
    response = client.post(
        "https://api.mstock.trade/openapi/typeb/orders/cancelall", headers=headers
    )

    # Add status attribute for compatibility
    response.status = response.status_code

    logger.debug(f"Cancel all response status code: {response.status_code}")
    logger.debug(f"Cancel all response: {response.text}")

    try:
        response_data = json.loads(response.text)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse cancel all response: {response.text}")
        return [], pending_order_ids  # Return as failed

    # Handle list response (like place order)
    if isinstance(response_data, list) and len(response_data) > 0:
        logger.debug("API returned list, extracting first element")
        response_data = response_data[0]

    # Check if the request was successful
    if _is_success(response_data.get("status")) or response_data.get("message") == "SUCCESS":
        logger.debug(f"Cancel all orders successful - cancelled {len(pending_order_ids)} orders")
        return pending_order_ids, []
    else:
        error_message = response_data.get("message", "Failed to cancel all orders")
        logger.error(f"Cancel all failed: {error_message}")
        return [], pending_order_ids  # Return as failed
