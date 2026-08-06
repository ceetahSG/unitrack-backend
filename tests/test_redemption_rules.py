"""When a scanned code becomes a deducted ride, and when it must not.

The interesting cases are all consequences of offline boarding. Two helper
phones with no signal cannot see each other's redemption logs, so the same code
can genuinely be accepted twice before either syncs. Cryptography cannot prevent
that — only recording it can — and how the server reacts decides whether a
passenger gets charged twice for one boarding, and whether an admin ever finds
out it happened.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.commerce import RedemptionFlag, TicketStatus
from app.services.redemption import RedemptionRejected, _check_valid_window


class _Ticket:
    """Only the fields the window check reads."""

    def __init__(self, status=TicketStatus.active, days_left=7, days_until_valid=0):
        now = datetime.now(UTC)
        self.status = status
        self.valid_from = now + timedelta(days=days_until_valid)
        self.valid_to = now + timedelta(days=days_left)


def test_an_active_in_date_ticket_passes() -> None:
    _check_valid_window(_Ticket(), datetime.now(UTC))


def test_a_revoked_ticket_is_refused() -> None:
    """Revocation is how a stolen or refunded ticket stops working."""
    with pytest.raises(RedemptionRejected, match="revoked"):
        _check_valid_window(_Ticket(status=TicketStatus.revoked), datetime.now(UTC))


def test_an_expired_ticket_is_refused_by_date_even_if_status_lags() -> None:
    """`status` is updated by a sweep, so the date is the real authority.

    Trusting the column alone would let a monthly pass keep working until some
    background job happened to notice.
    """
    with pytest.raises(RedemptionRejected, match="expired"):
        _check_valid_window(_Ticket(days_left=-1), datetime.now(UTC))


def test_a_ticket_not_yet_valid_is_refused() -> None:
    with pytest.raises(RedemptionRejected, match="not yet valid"):
        _check_valid_window(_Ticket(days_until_valid=2), datetime.now(UTC))


def test_running_out_of_rides_is_not_part_of_the_window_check() -> None:
    """The ordering here is load-bearing, and getting it wrong cost real
    evidence during testing.

    An exhausted ticket must still be able to *record* a duplicate: spec §7.5
    describes exactly the case where two offline devices push a ticket past
    zero, and that is the situation the fraud sweep exists to catch. If the
    ride count were checked here — before the duplicate is detected — the
    second device's report would be rejected outright and the only proof it
    happened would be discarded.

    So `_check_valid_window` deliberately says nothing about rides, and `redeem`
    applies that test only to a first sighting.
    """
    exhausted = _Ticket()
    exhausted.rides_remaining = 0

    # No exception: emptiness is not a reason to refuse to write history.
    _check_valid_window(exhausted, datetime.now(UTC))


def test_the_duplicate_flag_is_distinct_from_a_clean_boarding() -> None:
    """A flagged row must be findable. Sharing a value with `ok` would leave
    the fraud sweep nothing to query for."""
    assert RedemptionFlag.duplicate_suspect != RedemptionFlag.ok
    assert RedemptionFlag.duplicate_suspect.value == "duplicate_suspect"
