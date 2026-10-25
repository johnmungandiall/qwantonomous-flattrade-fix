# Mapping OpenAlgo API Request https://openalgo.in/docs
# Mapping Flattrade Broking Parameters https://piconnect.flattrade.in/docs/

import threading
import time

from broker.flattrade.api.data import BrokerData
from database.token_db import get_br_symbol, get_symbol_info
from utils.logging import get_logger
from utils.mpp_slab import (
    calculate_protected_price,
    custom_percentage_from,
    get_instrument_type_from_symbol,
    round_to_tick_size,
    slm_protected_price,
)

logger = get_logger(__name__)


# --- Reference price for MARKET emulation (retry + short TTL cache) ----------
# The Noren OMS REJECTS raw MKT / SL-MKT for API orders, so every MARKET order
# is rewritten to a LIMIT priced off a live quote. That quote fetch is therefore
# the single thing standing between a square-off and a dead order: during the
# 09:15 exit burst a transient quote failure silently degraded the order to
# `MKT`, and Flattrade rejected it (`0/390 Rejected`). A retry plus a short-lived
# per-contract cache makes the quote a best-effort dependency, and if it still
# cannot be priced the order now FAILS LOUDLY instead of being sent invalid.
_MPP_CACHE_TTL_SECONDS = 60.0
_MPP_QUOTE_ATTEMPTS = 2
_MPP_RETRY_GAP_SECONDS = 0.15
_mpp_price_cache = {}  # {(symbol, exchange): (quote_dict, monotonic_ts)}
_mpp_price_cache_lock = threading.Lock()


class MPPPriceUnavailable(Exception):
    """No usable reference price for a MARKET / SL-M order.

    Raised instead of falling back to `MKT`, which this broker's OMS rejects for
    API orders - a clear failure beats an order that is dead on arrival.
    """


def _remember_quote(symbol, exchange, quote):
    with _mpp_price_cache_lock:
        _mpp_price_cache[(symbol, exchange)] = (quote, time.monotonic())


def _cached_quote(symbol, exchange):
    with _mpp_price_cache_lock:
        entry = _mpp_price_cache.get((symbol, exchange))
    if not entry:
        return None
    quote, stored_at = entry
    if (time.monotonic() - stored_at) > _MPP_CACHE_TTL_SECONDS:
        return None
    return quote


def _tick_size_from_master(symbol, exchange):
    """Tick size from the instrument master, for when the quote omits it."""
    try:
        info = get_symbol_info(symbol, exchange)
        return getattr(info, "tick_size", None)
    except Exception as error:
        logger.error(f"MPP: tick size lookup failed for {symbol}@{exchange}: {error}")
        return None


def _reference_quote(symbol, exchange, auth_token):
    """Best-effort quote for MPP: one short retry, then a recent cached one.

    Returns a dict with ltp / bid / ask / tick_size, or None when nothing usable
    is available (the caller decides - see [MPPPriceUnavailable]).
    """
    last_error = None
    for attempt in range(_MPP_QUOTE_ATTEMPTS):
        try:
            quote = BrokerData(auth_token).get_quotes(symbol, exchange)
            if quote and float(quote.get("ltp") or 0) > 0:
                _remember_quote(symbol, exchange, quote)
                return quote
            last_error = f"no LTP in quote ({quote})"
        except Exception as error:
            last_error = str(error)
        if attempt + 1 < _MPP_QUOTE_ATTEMPTS:
            time.sleep(_MPP_RETRY_GAP_SECONDS)

    cached = _cached_quote(symbol, exchange)
    if cached is not None:
        logger.warning(
            f"MPP: live quote unavailable for {symbol}@{exchange} ({last_error}); "
            f"using cached reference price LTP={cached.get('ltp')} "
            f"bid={cached.get('bid')} ask={cached.get('ask')}"
        )
        return cached

    logger.error(
        f"MPP: NO reference price for {symbol}@{exchange} ({last_error}) - "
        f"this MARKET order cannot be priced"
    )
    # RAISE, never return a sentinel: the caller must not be able to send a raw
    # `MKT`, which this broker's OMS rejects for API orders.
    raise MPPPriceUnavailable(
        f"no usable quote for {symbol}@{exchange} ({last_error})"
    )


def _marketable_limit_price(
    quote, action, instrument_type, tick_size, custom_percentage=None
):
    """A limit price that actually crosses the spread.

    MPP alone prices the limit a fixed % away from the LTP, which is fine while
    the book is calm but can still rest BEHIND the best bid when the premium is
    moving fast (exactly what a square-off meets). So the protective price is
    combined with the touch: a SELL never rests above the best bid and a BUY
    never rests below the best ask. The rounding DIRECTION then keeps that
    marketability instead of giving half a tick back:
      SELL -> floor (never rounds UP into an unfillable price)
      BUY  -> ceil  (never rounds DOWN and fails to reach the offer)
    """
    ltp = float(quote.get("ltp") or 0)
    direction = "ceil" if action == "BUY" else "floor"

    protected = calculate_protected_price(
        price=ltp,
        action=action,
        instrument_type=instrument_type,
        tick_size=tick_size,
        round_direction=direction,
        custom_percentage=custom_percentage,
    )

    if action == "BUY":
        ask = float(quote.get("ask") or 0)
        if ask > 0 and ask > protected:
            return round_to_tick_size(ask, tick_size, "ceil")
    else:
        bid = float(quote.get("bid") or 0)
        if bid > 0 and bid < protected:
            return round_to_tick_size(bid, tick_size, "floor")

    return protected


def transform_data(data, token, auth_token=None):
    """
    Transforms the new API request structure to the current expected structure.
    For MARKET and SL-M orders, fetches quotes and adjusts price using MPP
    (Market Price Protection), then sends MARKET as LMT and SL-M as SL-LMT:
    - EQ/FUT: Price < 100: 2%, 100-500: 1%, > 500: 0.5%
    - OPT (CE/PE): Price < 10: 5%, 10-100: 3%, 100-500: 2%, > 500: 1%

    Args:
        data: Order data dictionary
        token: Instrument token
        auth_token: Authentication token for fetching quotes (passed from order_api)
    """
    symbol = get_br_symbol(data["symbol"], data["exchange"])
    # Handle special characters in symbol
    if symbol and "&" in symbol:
        symbol = symbol.replace("&", "%26")

    # Default values
    price = str(data.get("price", "0"))
    order_type = map_order_type(data["pricetype"])
    action = data["action"].upper()

    # Apply Market Price Protection for MARKET and SL-M orders.
    # Flattrade runs the same Noren OMS as Shoonya, which blocks both MKT and
    # SL-MKT price types for API orders. The two are NOT priced the same way,
    # because a stop-limit must keep its limit on the protective side of its OWN
    # trigger (Noren: prc > trgprc for a BUY, prc < trgprc for a SELL):
    #   MARKET -> LMT    priced off the LTP, crossed to the touch
    #   SL-M   -> SL-LMT priced off the TRIGGER, so the relation always holds
    # Pricing an SL-M off the LTP broke that relation whenever price and trigger
    # disagreed: a BUY triggered at 110 with the LTP at 100 went out at 102,
    # i.e. BELOW its own trigger, and the OMS rejected it.
    if data["pricetype"] in ("MARKET", "SL-M"):
        original_type = data["pricetype"]
        logger.info(
            f"MPP: {original_type} order detected for Symbol={data['symbol']}, Exchange={data['exchange']}, Action={action}"
        )
        try:
            if original_type == "SL-M":
                # An SL-LMT must sit strictly beyond its own trigger, so an SL-M
                # prices off the TRIGGER and never off the LTP / bid-ask touch.
                # No quote is needed either: the instrument master owns the tick
                # size, which keeps an SL-M independent of a live quote.
                trigger = float(data.get("trigger_price") or 0)
                tick_size = _tick_size_from_master(data["symbol"], data["exchange"])
                protected_price = slm_protected_price(
                    action=action,
                    trigger=trigger,
                    tick_size=tick_size,
                    custom_percentage=custom_percentage_from(data),
                    symbol=data["symbol"],
                    instrument_type=get_instrument_type_from_symbol(data["symbol"]),
                )
                price = str(protected_price)
                order_type = "SL-LMT"
                logger.info(
                    f"MPP SL-M Conversion Complete: Symbol={data['symbol']}, "
                    f"Trigger={trigger}, TickSize={tick_size}, "
                    f"OrderType={original_type}->{order_type}, FinalPrice={protected_price}"
                )
            elif auth_token:
                # Retry + short-TTL cache. The quote is the ONE dependency that
                # decides whether this MARKET order gets a valid limit price; a
                # transient failure used to degrade it to `MKT`, which the Noren
                # OMS rejects for API orders (the 09:15 `0/390 Rejected`).
                quote_data = _reference_quote(
                    data["symbol"], data["exchange"], auth_token
                )
                logger.info(
                    f"MPP Quote Response: Symbol={data['symbol']}, Exchange={data['exchange']}, "
                    f"LTP={quote_data.get('ltp')}, Bid={quote_data.get('bid')}, Ask={quote_data.get('ask')}, "
                    f"TickSize={quote_data.get('tick_size')}"
                )

                # Get instrument type from symbol
                instrument_type = get_instrument_type_from_symbol(data["symbol"])

                # Get tick_size from quote response; fall back to the instrument
                # master, because tick rounding is what keeps the limit valid.
                tick_size = quote_data.get("tick_size")
                if not tick_size:
                    tick_size = _tick_size_from_master(
                        data["symbol"], data["exchange"]
                    )
                logger.info(
                    f"MPP Symbol Info: InstrumentType={instrument_type}, TickSize={tick_size}"
                )

                # Get LTP for price calculation
                ltp = float(quote_data.get("ltp", 0))

                if ltp > 0:
                    # Marketable limit: MPP off the LTP, then crossed to the
                    # touch, then rounded in the direction that STAYS crossing
                    # (SELL floors, BUY ceils). A plain "% below LTP" can still
                    # rest behind the bid when the premium is falling fast, and
                    # nearest-tick rounding gave back half a tick.
                    protected_price = _marketable_limit_price(
                        quote_data,
                        action,
                        instrument_type,
                        tick_size,
                        custom_percentage_from(data),
                    )
                    price = str(protected_price)
                    # Only MARKET reaches this path - SL-M was priced off its own
                    # trigger above, so it never touches the LTP or the touch.
                    order_type = "LMT"

                    logger.info(
                        f"MPP Conversion Complete: Symbol={data['symbol']}, "
                        f"OrderType={original_type}->{order_type}, FinalPrice={protected_price}"
                    )
                else:
                    logger.warning(
                        f"MPP Warning: LTP is 0 or invalid for Symbol={data['symbol']}, "
                        f"Exchange={data['exchange']}. Proceeding with regular {original_type} order"
                    )
            else:
                logger.warning(
                    f"MPP Warning: No auth token available for Symbol={data['symbol']}. "
                    f"Cannot fetch quotes for MPP adjustment"
                )
        except MPPPriceUnavailable:
            # Do NOT degrade to a raw `MKT` - the guard below turns this into a
            # loud failure. Re-raised untouched so the reason survives.
            raise
        except Exception as e:
            logger.error(
                f"MPP Error: Failed to apply MPP for Symbol={data['symbol']}, "
                f"Exchange={data['exchange']}, Error={str(e)}. Proceeding with regular {original_type} order."
            )

        # Nothing that reaches here may go out as `MKT` or `SL-MKT`: this OMS
        # rejects both price types for API orders, so such an order is dead on
        # arrival while the caller still believes the position moved or closed -
        # which is exactly how three SELLs ended up `0/390 Rejected` at 09:15:24
        # and the app then entered the opposite leg. Fail loudly instead.
        if original_type == "SL-M" and order_type != "SL-LMT":
            raise MPPPriceUnavailable(
                f"cannot price an SL-M {action} for {data['symbol']} on "
                f"{data['exchange']} off trigger {data.get('trigger_price')!r} "
                f"- order NOT sent"
            )
        if original_type == "MARKET" and order_type != "LMT":
            raise MPPPriceUnavailable(
                f"cannot price a MARKET {action} for {data['symbol']} on "
                f"{data['exchange']} - no usable quote (order NOT sent)"
            )

    # Basic mapping - ensure all numeric values are strings
    transformed = {
        "uid": data["apikey"],
        "actid": data["apikey"],
        "exch": data["exchange"],
        "tsym": symbol,
        "qty": str(data["quantity"]),
        "prc": price,
        "trgprc": str(data.get("trigger_price", "0")),
        "dscqty": str(data.get("disclosed_quantity", "0")),
        "prd": map_product_type(data["product"]),
        "trantype": "B" if action == "BUY" else "S",
        "prctyp": order_type,
        "mkt_protection": "0",
        "ret": "DAY",
        "ordersource": "API",
    }

    # Log order data without sensitive fields (uid, actid contain API keys)
    safe_log = {k: v for k, v in transformed.items() if k not in ("uid", "actid")}
    logger.info(f"Transformed order data: {safe_log}")
    return transformed


def transform_modify_order_data(data, token, auth_token=None):
    """Build the ModifyOrder payload, applying the same MPP conversion as place.

    Noren's ModifyOrder accepts only LMT / SL-LMT, so a modify that says MARKET
    or SL-M has to be converted here exactly as the place path does - otherwise
    the modify is rejected. An SL-M modify is priced off its own TRIGGER for the
    same reason as at place time: the limit must sit beyond the trigger.

    Expects ``data["symbol"]`` in OpenAlgo format. The broker symbol is derived
    locally via get_br_symbol, which is what keeps the MPP quote / slab /
    tick-size lookups (all keyed on the OpenAlgo symbol) working.
    """
    oa_symbol = data["symbol"]
    exchange = data["exchange"]

    # Broker symbol for the outbound payload; handle & escaping.
    symbol = get_br_symbol(oa_symbol, exchange) or oa_symbol
    if symbol and "&" in symbol:
        symbol = symbol.replace("&", "%26")

    price = str(data["price"])
    pricetype = data["pricetype"]
    order_type = map_order_type(pricetype)
    action = (data.get("action") or "").upper()

    if pricetype in ("MARKET", "SL-M"):
        # The direction decides which side of the trigger the limit lands on, so
        # it cannot be guessed: a modify request without it is refused rather
        # than priced in the wrong direction.
        if action not in ("BUY", "SELL"):
            raise MPPPriceUnavailable(
                f"cannot convert a {pricetype} modify for {oa_symbol} without an "
                f"action (got {data.get('action')!r}) - order NOT sent"
            )

        if pricetype == "SL-M":
            # Off the TRIGGER, never the LTP - see transform_data.
            trigger = float(data.get("trigger_price") or 0)
            tick_size = _tick_size_from_master(oa_symbol, exchange)
            protected_price = slm_protected_price(
                action=action,
                trigger=trigger,
                tick_size=tick_size,
                custom_percentage=custom_percentage_from(data),
                symbol=oa_symbol,
                instrument_type=get_instrument_type_from_symbol(oa_symbol),
            )
            price = str(protected_price)
            order_type = "SL-LMT"
            logger.info(
                f"Modify MPP SL-M Conversion Complete: Symbol={oa_symbol}, "
                f"Trigger={trigger}, TickSize={tick_size}, "
                f"OrderType={pricetype}->{order_type}, FinalPrice={protected_price}"
            )
        else:
            if not auth_token:
                raise MPPPriceUnavailable(
                    f"cannot price a MARKET modify for {oa_symbol} without an "
                    f"auth token (order NOT sent)"
                )
            quote_data = _reference_quote(oa_symbol, exchange, auth_token)
            tick_size = quote_data.get("tick_size") or _tick_size_from_master(
                oa_symbol, exchange
            )
            protected_price = _marketable_limit_price(
                quote_data,
                action,
                get_instrument_type_from_symbol(oa_symbol),
                tick_size,
                custom_percentage_from(data),
            )
            price = str(protected_price)
            order_type = "LMT"
            logger.info(
                f"Modify MPP MARKET Conversion Complete: Symbol={oa_symbol}, "
                f"LTP={quote_data.get('ltp')}, TickSize={tick_size}, "
                f"OrderType={pricetype}->{order_type}, FinalPrice={protected_price}"
            )

    result = {
        "uid": data["apikey"],
        "exch": exchange,
        "norenordno": data["orderid"],
        "prctyp": order_type,
        "prc": price,
        "qty": str(data["quantity"]),
        "tsym": symbol,
        "ret": "DAY",
        "dscqty": str(data.get("disclosed_quantity") or 0),
    }

    # Only include trigger price for SL/SL-M orders
    # Sending trgprc=0 for LIMIT orders causes "Trigger price invalid - 0.00" error
    if data["pricetype"] in ["SL", "SL-M"]:
        result["trgprc"] = str(data.get("trigger_price") or 0)

    return result


def map_order_type(pricetype):
    """
    Maps the new pricetype to the existing order type.
    """
    order_type_mapping = {"MARKET": "MKT", "LIMIT": "LMT", "SL": "SL-LMT", "SL-M": "SL-MKT"}
    return order_type_mapping.get(pricetype, "MARKET")  # Default to MARKET if not found


def map_product_type(product):
    """
    Maps the new product type to the existing product type.
    """
    product_type_mapping = {
        "CNC": "C",
        "NRML": "M",
        "MIS": "I",
    }
    return product_type_mapping.get(product, "I")  # Default to DELIVERY if not found


def reverse_map_product_type(product):
    """
    Maps the new product type to the existing product type.
    """
    reverse_product_type_mapping = {
        "C": "CNC",
        "M": "NRML",
        "I": "MIS",
    }
    return reverse_product_type_mapping.get(product)
