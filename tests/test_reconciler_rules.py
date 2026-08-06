"""The reconciler's judgement calls, isolated from the database.

This job issues tickets for payments nobody reported, which means it can also
issue tickets for payments that never happened. These tests pin the two
decisions that stand between those outcomes: which gateway record counts as
money taken, and how long to wait before calling an order abandoned.
"""

from app.worker.payment_reconciler import (
    ABANDON_H,
    GRACE_S,
    INTERVAL_S,
    _successful_element,
)


def _element(status: str, amount: str = "100.00", val_id: str = "VAL123") -> dict:
    """One attempt as the gateway reports it.

    A real response puts a `val_id` on the successful attempt and an empty one
    on the failure, so the default mirrors that rather than omitting the field.
    """
    return {
        "status": status,
        "amount": amount,
        "currency": "BDT",
        # Empty on both in a real transaction-query response — see
        # test_the_transaction_query_shape_is_understood.
        "currency_amount": "",
        "currency_type": "",
        "val_id": val_id,
    }


def test_no_records_means_nothing_to_settle() -> None:
    """An empty response is the normal case for an abandoned checkout."""
    assert _successful_element([]) is None


def test_a_single_successful_attempt_is_found() -> None:
    assert _successful_element([_element("VALID")]) == _element("VALID")


def test_a_failed_attempt_alone_settles_nothing() -> None:
    assert _successful_element([_element("FAILED")]) is None
    assert _successful_element([_element("UNATTEMPTED")]) is None


def test_a_success_after_a_failure_is_still_found() -> None:
    """The ordinary retry: a card declines, the wallet works.

    Taking the first element blindly would read the decline as authoritative
    and leave a student who genuinely paid without a ticket.
    """
    elements = [_element("FAILED"), _element("VALID")]
    assert _successful_element(elements) == _element("VALID")


def test_a_failure_after_a_success_does_not_hide_the_success() -> None:
    """Order in the response is not a guarantee, so neither end is trusted."""
    elements = [_element("VALID"), _element("FAILED")]
    assert _successful_element(elements) == _element("VALID")


def test_validated_counts_as_money_taken() -> None:
    """Re-querying a settled transaction reports VALIDATED rather than VALID.

    The reconciler asks about old orders by definition, so this is the status it
    will usually see for a real payment.
    """
    assert _successful_element([_element("VALIDATED")]) == _element("VALIDATED")


def test_a_success_without_a_val_id_is_unusable() -> None:
    """The val_id is the handle for the validation call that actually settles.

    A success with no val_id cannot be confirmed, so treating it as payable
    would issue a ticket on the strength of a summary line.
    """
    assert _successful_element([{"status": "VALID", "val_id": ""}]) is None
    assert _successful_element([{"status": "VALID"}]) is None


def test_the_real_two_element_shape_from_a_bkash_payment() -> None:
    """Taken verbatim from a live sandbox bKash payment.

    Two things here surprised the first implementation: the failed attempt is
    listed first and carries the amount, while the successful one carries the
    val_id and an *empty* `currency_amount`. Settling from this element
    directly would fail the amount check and reject a payment that really
    happened — which is why the reconciler re-fetches via the validation API.
    """
    elements = [
        {
            "status": "FAILED",
            "currency_amount": "30.00",
            "currency_type": "BDT",
            "val_id": "",
            "bank_tran_id": "26080692347gqZREdr5rFNEYfi",
            "error": "Unattempted or Expired",
        },
        {
            "status": "VALID",
            "currency_amount": "",
            "currency_type": "",
            "val_id": "260806924146kskVrJQgU0qHVh",
            "bank_tran_id": "26080692414QzhgAWXPWKn2HUm",
            "card_type": "BKASH-BKash",
            "risk_level": "0",
        },
    ]

    found = _successful_element(elements)

    assert found is not None
    assert found["val_id"] == "260806924146kskVrJQgU0qHVh"
    assert found["card_type"] == "BKASH-BKash"


def test_the_grace_period_is_long_enough_to_pay_but_shorter_than_the_interval() -> None:
    """A fresh order must not be queried mid-payment, and must not wait a whole
    extra pass to be looked at once it is eligible."""
    assert GRACE_S >= 5 * 60
    assert GRACE_S <= INTERVAL_S


def test_orders_are_not_abandoned_the_same_day() -> None:
    """Abandoning too early marks a slow-but-real payment as failed.

    Anything under a full day risks catching a payment that a gateway settled
    overnight, so the threshold stays at least 24 hours.
    """
    assert ABANDON_H >= 24
