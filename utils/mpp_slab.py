# Market Price Protection (MPP) Slab Configuration
# Reference: https://support.zerodha.com/category/trading-and-markets/charts-and-orders/order/articles/market-price-protection-on-the-order-window
#
# This module provides centralized Market Price Protection functionality for OpenAlgo.
# When brokers stop supporting market orders, this converts MARKET orders to LIMIT orders
# with a price buffer based on configurable protection percentages.

import math
from decimal import Decimal
from typing import Optional

from utils.logging import get_logger

logger = get_logger(__name__)

# MPP Slabs for Equity and Futures (EQ and FUT)
# Based on Indian exchange regulations
# Format: (max_price, protection_percentage)
EQ_FUT_MPP_SLABS = [
    (100, 2.0),  # Price < 100: 2% protection
    (500, 1.0),  # Price 100-500: 1% protection
    (float("inf"), 0.5),  # Price > 500: 0.5% protection
]

# MPP Slabs for Options (CE and PE)
# Options have different slabs due to higher volatility
OPT_MPP_SLABS = [
    (10, 5.0),  # Price < 10: 5% protection
    (100, 3.0),  # Price 10-100: 3% protection
    (500, 2.0),  # Price 100-500: 2% protection
    (float("inf"), 1.0),  # Price > 500: 1% protection
]

# Instrument types that use Options slabs
OPTIONS_INSTRUMENT_TYPES = ["CE", "PE"]

# Hard ceiling for a CALLER-SUPPLIED protection percentage (`mpp_percentage` on
# the order APIs). A SELL limit is priced at `price * (1 - pct/100)`, so at
# 100 % the limit is zero and beyond it the limit turns negative - no exchange
# accepts either. The slab table's own maximum is 5 %, so every value the slabs
# produce sits far inside this bound; it exists only to keep a typed figure
# (e.g. 100 or 1000) from building an unsendable order.
MPP_PERCENTAGE_LIMIT = 100.0


def normalise_custom_percentage(percentage):
    """Validate a caller-supplied MPP percentage.

    Args:
        percentage: Raw value from the request (`mpp_percentage`). None, an
            empty string or a blank string all mean "not supplied" and return
            None, so the slab table keeps deciding.

    Returns:
        Optional[float]: The percentage to use, or None to fall back to the slab.

    Raises:
        ValueError: The value is not a finite number in (0, MPP_PERCENTAGE_LIMIT).
    """
    if percentage is None:
        return None
    if isinstance(percentage, str):
        percentage = percentage.strip()
        if percentage == "":
            return None
    try:
        value = float(percentage)
    except (TypeError, ValueError):
        raise ValueError(
            f"Invalid mpp_percentage {percentage!r}: must be a number"
        ) from None
    if not math.isfinite(value):
        raise ValueError(
            f"Invalid mpp_percentage {percentage!r}: must be a finite number"
        )
    if value <= 0:
        raise ValueError(
            f"Invalid mpp_percentage {value}: must be greater than 0 (omit it "
            f"to use the built-in slab)"
        )
    if value >= MPP_PERCENTAGE_LIMIT:
        raise ValueError(
            f"Invalid mpp_percentage {value}: must be below "
            f"{MPP_PERCENTAGE_LIMIT:g} % - a limit priced a full {MPP_PERCENTAGE_LIMIT:g} "
            f"% away collapses to zero"
        )
    return value


def custom_percentage_from(data):
    """Read the caller's `mpp_percentage` out of an OpenAlgo order dict.

    Every broker mapper already receives the validated order dict, so this is
    the one place that decides what an `mpp_percentage` key means. A missing or
    blank value returns None and the slab table keeps deciding, which is exactly
    the behaviour every existing install already has.
    """
    if not isinstance(data, dict):
        return None
    return normalise_custom_percentage(data.get("mpp_percentage"))


def get_instrument_type_from_symbol(symbol: str) -> str:
    """
    Determine instrument type from symbol name.

    Args:
        symbol: Trading symbol (e.g., 'RELIANCE', 'NIFTY24DEC25000CE', 'NIFTY24DECFUT')

    Returns:
        str: 'CE', 'PE', 'FUT', or 'EQ'
    """
    symbol_upper = symbol.upper()
    if symbol_upper.endswith("CE"):
        return "CE"
    elif symbol_upper.endswith("PE"):
        return "PE"
    elif symbol_upper.endswith("FUT"):
        return "FUT"
    else:
        return "EQ"


def get_mpp_slabs(instrument_type: str) -> list:
    """
    Get the appropriate MPP slabs based on instrument type.

    Args:
        instrument_type: 'EQ', 'FUT', 'CE', or 'PE'

    Returns:
        list: The MPP slabs to use
    """
    if instrument_type in OPTIONS_INSTRUMENT_TYPES:
        return OPT_MPP_SLABS
    else:
        return EQ_FUT_MPP_SLABS


def get_mpp_percentage(
    price: float, instrument_type: str = "EQ", custom_percentage=None
) -> float:
    """
    Get the Market Price Protection percentage for a given price and instrument type.

    Args:
        price: The current market price (LTP)
        instrument_type: 'EQ', 'FUT', 'CE', or 'PE'
        custom_percentage: Caller-supplied percentage. When given (and valid) it
            replaces the slab lookup entirely, so the caller decides the buffer.

    Returns:
        float: The protection percentage to apply

    Example:
        >>> get_mpp_percentage(50, 'EQ')   # Returns 2.0 (for EQ/FUT price < 100)
        >>> get_mpp_percentage(50, 'CE')   # Returns 3.0 (for OPT price 10-100)
        >>> get_mpp_percentage(5, 'PE')    # Returns 5.0 (for OPT price < 10)
        >>> get_mpp_percentage(50, 'EQ', custom_percentage=0.75)  # Returns 0.75
    """
    custom = normalise_custom_percentage(custom_percentage)
    if custom is not None:
        logger.info(
            f"MPP Percentage (caller-supplied): InstrumentType={instrument_type}, "
            f"Price={price}, Protection={custom}%"
        )
        return custom

    slabs = get_mpp_slabs(instrument_type)
    slab_type = "OPT" if instrument_type in OPTIONS_INSTRUMENT_TYPES else "EQ/FUT"

    # Find the appropriate slab for the price
    for max_price, percentage in slabs:
        if price < max_price:
            slab_desc = f"< {max_price}" if max_price != float("inf") else "> 500"
            logger.info(
                f"MPP Slab Lookup: InstrumentType={instrument_type}, Price={price}, "
                f"Slab={slab_desc}, Protection={percentage}%, SlabType={slab_type}"
            )
            return percentage


def round_to_tick_size(price: float, tick_size: float = None, direction: str = "nearest") -> float:
    """
    Round price to a valid tick size.

    Args:
        price: The calculated price
        tick_size: The tick size for the instrument (from database)
        direction: "nearest" (default, every pre-existing caller), "floor" or
            "ceil". MARKET orders emulate a limit, so the rounding DIRECTION is
            what decides whether the limit stays marketable: a SELL that rounds
            UP by half a tick can sit unfilled behind the bid.

    Returns:
        float: Price rounded to the requested tick, or 2 decimal places if no
        tick size is known

    Example:
        >>> round_to_tick_size(102.0111, 0.05)  # Returns 102.0
        >>> round_to_tick_size(102.0111, 0.01)  # Returns 102.01
        >>> round_to_tick_size(102.0111, None)  # Returns 102.01 (2 decimal places)
        >>> round_to_tick_size(97.98, 0.05, "floor")  # Returns 97.95
    """
    mode = (direction or "nearest").lower()

    if tick_size is None or tick_size <= 0:
        # No tick size available, just round to 2 decimal places
        if mode == "floor":
            return round(math.floor(round(price * 100, 6)) / 100, 2)
        if mode == "ceil":
            return round(math.ceil(round(price * 100, 6)) / 100, 2)
        return round(price, 2)

    # `round(..., 6)` first: 102.0/0.05 is 2039.9999999999998 in binary float,
    # and flooring that naive value would drop a whole tick.
    ticks = round(price / tick_size, 6)
    if mode == "floor":
        rounded = math.floor(ticks) * tick_size
    elif mode == "ceil":
        rounded = math.ceil(ticks) * tick_size
    else:
        rounded = round(ticks) * tick_size

    # Ensure 2 decimal places for display
    return round(rounded, 2)


def calculate_protected_price(
    price: float,
    action: str,
    symbol: str = None,
    instrument_type: str = None,
    tick_size: float = None,
    custom_percentage: float = None,
    round_direction: str = "nearest",
) -> float:
    """
    Calculate the protected limit price for a market order with tick size rounding.

    Args:
        price: The current market price (LTP)
        action: Order action - 'BUY' or 'SELL'
        symbol: Trading symbol (used to determine instrument type if not provided)
        instrument_type: 'EQ', 'FUT', 'CE', or 'PE' (if None, derived from symbol)
        tick_size: Tick size for price rounding (from database)
        custom_percentage: Optional custom percentage to override slab-based calculation

    Returns:
        float: The adjusted limit price with protection, rounded to tick size

    Example:
        >>> calculate_protected_price(100, 'BUY', instrument_type='EQ')  # ~102.0 (100 + 2%)
        >>> calculate_protected_price(5, 'BUY', instrument_type='CE')    # ~5.25 (5 + 5%)
    """
    # Determine instrument type from symbol if not provided
    if instrument_type is None and symbol:
        instrument_type = get_instrument_type_from_symbol(symbol)
    elif instrument_type is None:
        instrument_type = "EQ"  # Default to EQ

    # Get protection percentage. A caller-supplied value wins over the slab.
    custom = normalise_custom_percentage(custom_percentage)
    if custom is not None:
        percentage = custom
        logger.info(f"MPP: Using caller-supplied percentage: {percentage}%")
    else:
        percentage = get_mpp_percentage(price, instrument_type)

    multiplier = percentage / 100
    price_adjustment = round(price * multiplier, 2)

    if action.upper() == "BUY":
        # For BUY orders, add protection percentage to ensure execution
        protected_price = price * (1 + multiplier)
        adjustment_type = "+"
    else:
        # For SELL orders, subtract protection percentage to ensure execution
        protected_price = price * (1 - multiplier)
        adjustment_type = "-"

    # A large caller-supplied percentage can drive the limit to zero or below
    # (SELL: `price * (1 - pct/100)`). Refuse instead of handing the broker an
    # unsendable price - the slab table can never reach this branch.
    if protected_price <= 0:
        raise ValueError(
            f"mpp_percentage {percentage}% prices a {action.upper()} at "
            f"{protected_price} for {symbol or instrument_type} (base {price}); "
            f"no positive limit can be derived from this percentage"
        )

    # Round to tick size. The direction matters: a marketable SELL must round
    # DOWN and a marketable BUY must round UP, so protection is never eaten by
    # half a tick of rounding toward the market.
    protected_price = round_to_tick_size(protected_price, tick_size, round_direction)

    # Rounding down a sub-tick SELL limit can still land on zero.
    if protected_price <= 0:
        raise ValueError(
            f"mpp_percentage {percentage}% leaves no valid tick for a "
            f"{action.upper()} on {symbol or instrument_type} (base {price}, "
            f"tick {tick_size})"
        )

    logger.info(
        f"MPP Calculation: Symbol={symbol or 'N/A'}, InstrumentType={instrument_type}, "
        f"Action={action.upper()}, BasePrice={price}, Protection={percentage}%, "
        f"Adjustment={adjustment_type}{price_adjustment}, TickSize={tick_size}, "
        f"ProtectedPrice={protected_price}"
    )

    return protected_price


def tick_decimals(tick: float) -> int:
    """Decimal places implied by a tick size (0.05 -> 2, 0.0025 -> 4)."""
    return max(0, -Decimal(str(tick)).as_tuple().exponent)


def snap_to_tick(value: float, tick: float, direction: str) -> float:
    """Snap ``value`` to a multiple of ``tick``, in a FORCED direction.

    ``direction`` is "floor" or "ceil". Nearest-tick rounding is deliberately
    not offered here: on a trigger-anchored price it can put the limit back onto
    the trigger, which is exactly the invalid state these callers are avoiding.
    """
    ratio = round(value / tick, 6)  # tame float noise before the floor/ceil
    k = math.floor(ratio) if direction == "floor" else math.ceil(ratio)
    return round(k * tick, tick_decimals(tick))


def snap_beyond_trigger(limit, trigger, action, tick_size=None):
    """Force a stop-limit strictly beyond its own trigger, by at least a tick.

    A stop-limit lives on the PROTECTIVE side of its trigger — BUY above it,
    SELL below it — and every Noren-family / SEBI-MPP broker rejects an order
    that violates that relation with something like "Trigger price invalid".
    A buffer thinner than one tick rounds straight back onto the trigger, so the
    limit is stepped a full tick out and the strict side is then re-checked.

    Raises:
        ValueError: the required side cannot be reached with the given tick.
    """
    trigger = float(trigger)
    action = (action or "").upper()
    step = tick_size if (tick_size and tick_size > 0) else None

    if action == "BUY":
        if limit <= trigger and step:
            limit = snap_to_tick(trigger + step, tick_size, "ceil")
        if limit <= trigger:
            raise ValueError(
                f"cannot price an SL BUY limit above its trigger {trigger} "
                f"(tick {tick_size})"
            )
    else:
        if limit >= trigger and step:
            limit = snap_to_tick(trigger - step, tick_size, "floor")
        if limit >= trigger or limit <= 0:
            raise ValueError(
                f"cannot price an SL SELL limit below its trigger {trigger} "
                f"(tick {tick_size})"
            )
    return limit


def slm_protected_price(
    action: str,
    trigger: float,
    tick_size: float = None,
    custom_percentage=None,
    symbol: str = None,
    instrument_type: str = None,
    require_tick_size: bool = False,
) -> float:
    """Protective LIMIT price for an SL-M order that must go out as a stop-limit.

    A stop-limit keeps its LIMIT on the PROTECTIVE side of its own TRIGGER:
    a BUY needs `prc > trgprc` (so the limit fills once the trigger is hit) and
    a SELL needs `prc < trgprc`. Pricing such an order off the LTP instead
    breaks that relation whenever price and trigger disagree - a BUY triggered
    at 110 with an LTP of 100 went out at 102, i.e. BELOW its own trigger, and
    the OMS rejected it.

    So the reference price here is the TRIGGER, never the LTP, and the rounding
    direction is chosen to keep the limit beyond it: BUY ceils, SELL floors.
    This is the ONE implementation the whole codebase uses - do not re-derive it
    per broker.

    Args:
        action: 'BUY' or 'SELL'.
        trigger: The caller's trigger price (required, must be positive).
        tick_size: Instrument tick size; falls back to 2 decimal places.
        custom_percentage: Caller-supplied MPP % (None -> slab table).
        symbol / instrument_type: Used for the slab lookup only.
        require_tick_size: Fail closed when no tick size is known instead of
            falling back to 2 decimals. Set by brokers that reject an off-tick
            price (Dhan, Kotak, IIFL, 5Paisa).

    Returns:
        float: A tick-valid limit strictly beyond the trigger.

    Raises:
        ValueError: no trigger, an unknown action, a missing required tick
            size, or no valid limit can be derived from the trigger.
    """
    trigger = float(trigger or 0)
    if trigger <= 0:
        raise ValueError(f"SL-M needs a positive trigger price (got {trigger})")

    action = (action or "").upper()
    if action not in ("BUY", "SELL"):
        raise ValueError(f"SL-M needs a BUY/SELL action (got {action!r})")

    has_tick = bool(tick_size and tick_size > 0)
    if require_tick_size and not has_tick:
        raise ValueError(
            f"Cannot resolve a valid tick size for {symbol or 'the instrument'}; "
            f"required to build an SL-M protective limit price"
        )

    if instrument_type is None and symbol:
        instrument_type = get_instrument_type_from_symbol(symbol)

    # BUY ceils above the trigger, SELL floors below it.
    direction = "ceil" if action == "BUY" else "floor"
    protected = calculate_protected_price(
        price=trigger,
        action=action,
        symbol=symbol,
        instrument_type=instrument_type,
        tick_size=tick_size,
        custom_percentage=custom_percentage,
        round_direction=direction,
    )

    return snap_beyond_trigger(protected, trigger, action, tick_size)


def get_mpp_info(
    price: float,
    symbol: str = None,
    instrument_type: str = None,
    tick_size: float = None,
    custom_percentage=None,
) -> dict:
    """
    Get detailed MPP information for a given price.

    Args:
        price: The current market price
        symbol: Trading symbol
        instrument_type: 'EQ', 'FUT', 'CE', or 'PE'
        tick_size: Tick size for rounding (from database)

    Returns:
        dict: Dictionary with MPP details
    """
    if instrument_type is None and symbol:
        instrument_type = get_instrument_type_from_symbol(symbol)
    elif instrument_type is None:
        instrument_type = "EQ"

    custom = normalise_custom_percentage(custom_percentage)
    percentage = get_mpp_percentage(price, instrument_type, custom_percentage=custom)
    slab_type = "OPT" if instrument_type in OPTIONS_INSTRUMENT_TYPES else "EQ/FUT"

    return {
        "base_price": price,
        "symbol": symbol,
        "instrument_type": instrument_type,
        "slab_type": slab_type,
        "percentage": percentage,
        "custom_percentage": custom,
        "tick_size": tick_size,
        "buy_price": calculate_protected_price(
            price, "BUY", symbol, instrument_type, tick_size, custom
        ),
        "sell_price": calculate_protected_price(
            price, "SELL", symbol, instrument_type, tick_size, custom
        ),
    }


def log_mpp_slabs():
    """Log all MPP slabs for reference."""
    logger.info("=" * 50)
    logger.info("MPP Slabs for EQ and FUT (Equity & Futures):")
    logger.info("-" * 50)
    prev_max = 0
    for max_price, percentage in EQ_FUT_MPP_SLABS:
        if max_price == float("inf"):
            logger.info(f"  Price >= {prev_max}: {percentage}%")
        else:
            logger.info(f"  Price < {max_price}: {percentage}%")
        prev_max = max_price

    logger.info("=" * 50)
    logger.info("MPP Slabs for OPT (Options - CE/PE):")
    logger.info("-" * 50)
    prev_max = 0
    for max_price, percentage in OPT_MPP_SLABS:
        if max_price == float("inf"):
            logger.info(f"  Price >= {prev_max}: {percentage}%")
        else:
            logger.info(f"  Price < {max_price}: {percentage}%")
        prev_max = max_price
    logger.info("=" * 50)
