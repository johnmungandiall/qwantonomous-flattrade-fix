"""IOC (Immediate-or-Cancel) order validity — end-to-end server coverage.

Three layers are pinned here, in the order an order travels through them:

1. ``restx_api.schemas`` — the REST contract. ``validity`` is optional, defaults
   to ``DAY``, is case-insensitively normalised, and anything else is rejected
   with a 400-shaped ``ValidationError``.
2. ``services.place_order_service.validate_order_data`` — the service-level
   guard for callers that assemble an order dict without the schema.
3. ``broker.flattrade.mapping.transform_data`` — the broker's ``ret`` retention,
   including the MPP MARKET -> marketable-LMT rewrite, which must PRESERVE the
   caller's retention instead of resetting it to DAY.

Plus the analyzer/sandbox path, which must keep placing orders when
``validity`` is present in the payload.
"""

import pytest
from marshmallow import ValidationError

from broker.flattrade.mapping import transform_data as ft_map
from restx_api.schemas import (
    BasketOrderItemSchema,
    ModifyOrderSchema,
    OrderSchema,
    SmartOrderSchema,
    SplitOrderSchema,
)
from services.place_order_service import validate_order_data
from utils.constants import DEFAULT_VALIDITY, VALID_VALIDITY

BASE_ORDER = {
    "apikey": "test-api-key",
    "strategy": "n8n",
    "exchange": "NFO",
    "symbol": "NIFTY28OCT2625500CE",
    "action": "BUY",
    "quantity": 75,
}


# ── 1. Schema contract ───────────────────────────────────────────────────────


class TestOrderSchemaValidity:
    def test_constants_expose_the_supported_set(self):
        assert DEFAULT_VALIDITY == "DAY"
        assert VALID_VALIDITY == ["DAY", "IOC"]

    def test_omitted_validity_defaults_to_day(self):
        loaded = OrderSchema().load(dict(BASE_ORDER))
        assert loaded["validity"] == "DAY"

    def test_explicit_day_is_accepted(self):
        loaded = OrderSchema().load(dict(BASE_ORDER, validity="DAY"))
        assert loaded["validity"] == "DAY"

    def test_ioc_is_accepted_and_upper_cased(self):
        loaded = OrderSchema().load(dict(BASE_ORDER, validity="IOC"))
        assert loaded["validity"] == "IOC"

    @pytest.mark.parametrize("raw", ["ioc", "Ioc", " ioc ", "\tIOC\n"])
    def test_lower_and_padded_values_normalise(self, raw):
        loaded = OrderSchema().load(dict(BASE_ORDER, validity=raw))
        assert loaded["validity"] == "IOC"

    def test_none_means_not_supplied(self):
        # An explicit null is treated exactly like an omitted key, so a client
        # that serialises absent optionals as null still gets the default.
        loaded = OrderSchema().load(dict(BASE_ORDER, validity=None))
        assert loaded["validity"] == "DAY"

    @pytest.mark.parametrize("bad", ["GTC", "EOS", "", "IMMEDIATE", "DAY/IOC", "0"])
    def test_unknown_validity_is_rejected(self, bad):
        with pytest.raises(ValidationError) as err:
            OrderSchema().load(dict(BASE_ORDER, validity=bad))
        assert "validity" in str(err.value)

    def test_each_order_schema_carries_the_field(self):
        """Smart / Modify / Basket / Split all accept and normalise IOC."""
        smart = SmartOrderSchema().load(
            dict(BASE_ORDER, position_size=75, validity="ioc")
        )
        assert smart["validity"] == "IOC"

        modify = ModifyOrderSchema().load(
            dict(
                BASE_ORDER,
                orderid="OID-1",
                product="MIS",
                pricetype="LIMIT",
                price=12.5,
                disclosed_quantity=0,
                trigger_price=0,
                validity="ioc",
            )
        )
        assert modify["validity"] == "IOC"

        basket = BasketOrderItemSchema().load(
            {
                "exchange": "NFO",
                "symbol": "NIFTY28OCT2625500CE",
                "action": "BUY",
                "quantity": 75,
                "validity": "ioc",
            }
        )
        assert basket["validity"] == "IOC"

        split = SplitOrderSchema().load(
            dict(BASE_ORDER, splitsize=25, validity="ioc")
        )
        assert split["validity"] == "IOC"

    def test_each_order_schema_defaults_to_day(self):
        assert SmartOrderSchema().load(
            dict(BASE_ORDER, position_size=75)
        )["validity"] == "DAY"
        assert BasketOrderItemSchema().load(
            {
                "exchange": "NFO",
                "symbol": "X",
                "action": "BUY",
                "quantity": 75,
            }
        )["validity"] == "DAY"
        assert SplitOrderSchema().load(
            dict(BASE_ORDER, splitsize=25)
        )["validity"] == "DAY"


# ── 2. Service-level validation ─────────────────────────────────────────────


class TestServiceValidation:
    @staticmethod
    def _call(validity_marker="__absent__"):
        data = dict(BASE_ORDER)
        if validity_marker != "__absent__":
            data["validity"] = validity_marker
        return validate_order_data(data)

    def test_absent_validity_is_not_an_error(self):
        # Legacy callers never sent the key; they must keep working untouched.
        ok, _, message = self._call()
        assert ok is True
        assert message is None

    def test_ioc_lowercase_is_normalised_in_place(self):
        data = dict(BASE_ORDER, validity="ioc")
        ok, validated, _ = validate_order_data(data)
        assert ok is True
        assert validated["validity"] == "IOC"
        assert data["validity"] == "IOC", "the dict is normalised for the mapper"

    def test_invalid_validity_is_refused_with_a_clear_message(self):
        ok, validated, message = self._call("GTC")
        assert ok is False
        assert validated is None
        assert message is not None
        assert "Invalid validity" in message
        assert "DAY" in message and "IOC" in message


# ── 3. Flattrade broker mapping (`ret`) ─────────────────────────────────────


@pytest.fixture
def patched_mapping(monkeypatch):
    """Neutralise the two I/O dependencies of ``transform_data``.

    ``get_br_symbol`` hits the symbol master and ``_reference_quote`` the broker;
    neither is what these tests are about. The quote is stubbed with a full book
    (ltp/bid/ask/tick) so the MPP rewrite produces a real marketable limit.
    """
    monkeypatch.setattr(ft_map, "get_br_symbol", lambda symbol, exchange: symbol)
    monkeypatch.setattr(
        ft_map,
        "_reference_quote",
        lambda symbol, exchange, auth: {
            "ltp": 100.0,
            "bid": 99.5,
            "ask": 100.5,
            "tick_size": 0.05,
        },
    )
    return ft_map


class TestFlattradeRetention:
    def test_default_is_day(self, patched_mapping):
        # A plain LIMIT needs no quote, so this exercises the base mapping.
        out = patched_mapping.transform_data(
            dict(BASE_ORDER, pricetype="LIMIT", price=12.5, product="MIS"),
            token="1234",
        )
        assert out["ret"] == "DAY"

    def test_ioc_is_sent(self, patched_mapping):
        out = patched_mapping.transform_data(
            dict(
                BASE_ORDER,
                pricetype="LIMIT",
                price=12.5,
                product="MIS",
                validity="IOC",
            ),
            token="1234",
        )
        assert out["ret"] == "IOC"

    def test_lowercase_ioc_is_normalised(self, patched_mapping):
        out = patched_mapping.transform_data(
            dict(
                BASE_ORDER,
                pricetype="LIMIT",
                price=12.5,
                product="MIS",
                validity="ioc",
            ),
            token="1234",
        )
        assert out["ret"] == "IOC"

    def test_an_unknown_value_can_never_reach_the_oms(self, patched_mapping):
        # The schema rejects it, but internal callers assemble dicts directly.
        # Clamping means a bad string degrades to the legacy retention instead of
        # producing an order the broker refuses.
        out = patched_mapping.transform_data(
            dict(
                BASE_ORDER,
                pricetype="LIMIT",
                price=12.5,
                product="MIS",
                validity="GTC",
            ),
            token="1234",
        )
        assert out["ret"] == "DAY"

    def test_mpp_market_rewrite_preserves_ioc(self, patched_mapping):
        """The MARKET -> marketable-LMT rewrite must not reset the retention.

        This is the whole point of the change: a marketable limit with DAY
        retention RESTS unfilled behind the touch; with IOC the exchange cancels
        the remainder instead.
        """
        out = patched_mapping.transform_data(
            dict(BASE_ORDER, pricetype="MARKET", product="MIS", validity="IOC"),
            token="1234",
            auth_token="auth-token",
        )
        assert out["prctyp"] == "LMT", "Noren rejects a raw MKT for API orders"
        assert out["ret"] == "IOC", "the rewrite must not reset retention to DAY"
        assert float(out["prc"]) > 0

    def test_mpp_market_rewrite_keeps_day(self, patched_mapping):
        out = patched_mapping.transform_data(
            dict(BASE_ORDER, pricetype="MARKET", product="MIS"),
            token="1234",
            auth_token="auth-token",
        )
        assert out["prctyp"] == "LMT"
        assert out["ret"] == "DAY"

    def test_modify_mapping_reads_validity(self, patched_mapping):
        out = patched_mapping.transform_modify_order_data(
            dict(
                BASE_ORDER,
                orderid="OID-1",
                pricetype="LIMIT",
                price=12.5,
                product="MIS",
                validity="IOC",
            ),
            token="1234",
            auth_token="auth-token",
        )
        assert out["ret"] == "IOC"

    def test_modify_mapping_defaults_to_day(self, patched_mapping):
        out = patched_mapping.transform_modify_order_data(
            dict(
                BASE_ORDER,
                orderid="OID-1",
                pricetype="LIMIT",
                price=12.5,
                product="MIS",
            ),
            token="1234",
            auth_token="auth-token",
        )
        assert out["ret"] == "DAY"


# ── 4. Analyzer / sandbox compatibility ─────────────────────────────────────


class TestSandboxCompatibility:
    def test_sandbox_place_order_accepts_a_validity_key(self, monkeypatch):
        """Paper mode must keep working when the caller sends ``validity``.

        The sandbox converts the API order into its OWN dict and simply does not
        copy ``validity`` — it is a live-broker retention concept and the sandbox
        match engine fills immediately. What matters is that the extra key is
        ignored rather than rejected, and that the field set the sandbox requires
        is untouched.
        """
        captured = {}

        class _FakeOrderManager:
            def __init__(self, _user_id):
                pass

            def place_order(self, sandbox_order_data, prefetched_quote=None):
                captured.update(sandbox_order_data)
                return True, {"status": "success", "orderid": "SB-1"}, 200

        from services import sandbox_service

        monkeypatch.setattr(
            sandbox_service, "get_user_id_from_apikey", lambda api_key: "user-1"
        )
        monkeypatch.setattr(sandbox_service, "OrderManager", _FakeOrderManager)

        ok, response, status = sandbox_service.sandbox_place_order(
            dict(
                BASE_ORDER,
                pricetype="MARKET",
                product="MIS",
                validity="IOC",
            ),
            "test-api-key",
            dict(BASE_ORDER, validity="IOC"),
        )

        assert ok is True
        assert status == 200
        assert response["orderid"] == "SB-1"
        # The sandbox got everything it needs to place a paper order.
        assert captured["symbol"] == BASE_ORDER["symbol"]
        assert captured["price_type"] == "MARKET"
        assert captured["product"] == "MIS"
