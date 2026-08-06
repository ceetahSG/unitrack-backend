"""The checks that stand between a forged redirect and a free ticket.

The student comes back from SSLCommerz via a browser redirect, which is just an
HTTP request anyone can type by hand. Nothing in it is trusted. What decides
whether a ticket is issued is the server-to-server validation response, and
these three predicates are how that response is read.

Each test here is a way of getting a ticket without paying for one.
"""

from app.core.sslcommerz import amount_matches, payment_succeeded, risk_flagged


def _validation(**overrides):
    """A response for a genuine 100.00 BDT payment."""
    base = {
        "status": "VALID",
        "currency_amount": "100.00",
        "currency_type": "BDT",
        "risk_level": "0",
        "bank_tran_id": "BANK123",
    }
    return {**base, **overrides}


# --- status ----------------------------------------------------------------


def test_valid_is_a_payment() -> None:
    assert payment_succeeded(_validation())


def test_validated_is_also_a_payment() -> None:
    """SSLCommerz answers VALIDATED when a val_id is checked a second time.

    That happens every time a student refreshes the success page. Rejecting it
    would fail a payment that genuinely completed.
    """
    assert payment_succeeded(_validation(status="VALIDATED"))


def test_failed_and_unknown_statuses_are_not_payments() -> None:
    for bad in ("FAILED", "CANCELLED", "PENDING", "", "UNKNOWN"):
        assert not payment_succeeded(_validation(status=bad)), bad


def test_status_check_is_case_insensitive() -> None:
    assert payment_succeeded(_validation(status="valid"))


# --- amount ----------------------------------------------------------------


def test_matching_amount_and_currency_passes() -> None:
    assert amount_matches(_validation(), 10000, "BDT")


def test_paying_less_than_the_order_is_rejected() -> None:
    """The attack this exists for.

    A gateway response can be genuinely VALID for an amount the attacker chose.
    Checking only `status` would hand over a 100 BDT ticket for 1 BDT.
    """
    assert not amount_matches(_validation(currency_amount="1.00"), 10000, "BDT")


def test_paying_more_is_also_rejected() -> None:
    """Not a windfall — an amount we did not ask for means the response does not
    belong to this order, and issuing against it hides a real problem."""
    assert not amount_matches(_validation(currency_amount="500.00"), 10000, "BDT")


def test_a_different_currency_is_rejected() -> None:
    """100.00 USD is not 100.00 BDT. Comparing only the number would accept it."""
    assert not amount_matches(_validation(currency_type="USD"), 10000, "BDT")


def test_amounts_are_compared_in_paisa_not_floats() -> None:
    """0.1 + 0.2 != 0.3 in binary floating point.

    Amounts that cannot be represented exactly must still compare equal, which
    is why the comparison rounds to integer paisa rather than testing floats.
    """
    assert amount_matches(_validation(currency_amount="0.30"), 30, "BDT")
    assert amount_matches(_validation(currency_amount="1234.56"), 123456, "BDT")


def test_the_transaction_query_shape_is_understood() -> None:
    """The reconciler's responses fill different fields to the validation API's.

    Taken from a real sandbox bKash payment: `currency_amount` and
    `currency_type` come back **empty**, and the figure lives in `amount` /
    `currency` instead. Reading only the first pair made a genuine payment look
    like an amount mismatch, so the reconciler refused to issue a ticket for
    money that had actually moved.
    """
    from_query = {
        "status": "VALIDATED",
        "amount": "30.00",
        "currency": "BDT",
        "currency_amount": "",
        "currency_type": "",
        "store_amount": "29.25",
        "card_type": "BKASH-BKash",
    }
    assert amount_matches(from_query, 3000, "BDT")
    assert not amount_matches(from_query, 999999, "BDT")
    assert not amount_matches(from_query, 3000, "USD")


def test_store_amount_is_never_what_gets_compared() -> None:
    """`store_amount` is the merchant's take after the gateway's commission.

    On a 30.00 sale it reads 29.25. Comparing that against the order would
    reject every real payment, so it must not be reachable as a fallback.
    """
    fees_only = {"status": "VALID", "store_amount": "29.25", "currency": "BDT"}
    assert not amount_matches(fees_only, 3000, "BDT")
    assert not amount_matches(fees_only, 2925, "BDT")


def test_a_malformed_or_missing_amount_is_rejected() -> None:
    """A response we cannot read is not a response we can act on."""
    assert not amount_matches({"status": "VALID"}, 10000, "BDT")
    assert not amount_matches(_validation(currency_amount="not-a-number"), 10000, "BDT")
    assert not amount_matches(_validation(currency_amount=None), 10000, "BDT")


# --- risk ------------------------------------------------------------------


def test_risk_flagged_transactions_are_detected() -> None:
    """risk_level "1" means SSLCommerz wants a human to look.

    Issuing anyway is how a chargeback turns into a ticket that was never paid
    for, so the caller holds the order instead of settling it.
    """
    assert risk_flagged(_validation(risk_level="1"))


def test_normal_transactions_are_not_flagged() -> None:
    assert not risk_flagged(_validation())
    assert not risk_flagged(_validation(risk_level="0"))
