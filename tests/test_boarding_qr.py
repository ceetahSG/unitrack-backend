"""What makes a boarding QR valid, and every way one can be faked.

Each test here is an attempt to ride without a ticket: forging a code, reusing
someone else's, replaying an old one, editing the passenger count. The QR is
the only thing standing between a paid ticket and a free ride, and it has to
hold up on a helper's phone with no signal and no way to ask the server.
"""

import time

import pytest

from app.services.boarding import (
    SLICE_SECONDS,
    SLICE_TOLERANCE,
    InvalidQr,
    QrPayload,
    build_qr,
    current_slice,
    generate_keypair,
    new_nonce,
    parse_qr,
    verify_qr,
)

TICKET = "8626e7c2-4b60-42a4-be68-ce877496c9c0"


@pytest.fixture
def keys():
    return generate_keypair()


def _payload(now: float, passengers: int = 1) -> QrPayload:
    return QrPayload(
        ticket_id=TICKET,
        passenger_count=passengers,
        time_slice=current_slice(now),
        nonce=new_nonce(),
    )


# --- the happy path ---------------------------------------------------------


def test_a_freshly_signed_code_verifies(keys) -> None:
    private, public = keys
    now = time.time()

    payload = verify_qr(build_qr(private, _payload(now)), public, now)

    assert payload.ticket_id == TICKET
    assert payload.passenger_count == 1


def test_the_payload_survives_the_round_trip(keys) -> None:
    """Whatever the student's device encodes is what the helper reads."""
    private, public = keys
    now = time.time()
    original = _payload(now, passengers=3)

    decoded = verify_qr(build_qr(private, original), public, now)

    assert decoded == original


# --- forgery ----------------------------------------------------------------


def test_another_tickets_key_cannot_sign_this_ticket(keys) -> None:
    """The core guarantee. Holding *a* ticket must not let you mint codes for
    someone else's."""
    _, public = keys
    other_private, _ = generate_keypair()
    now = time.time()

    with pytest.raises(InvalidQr, match="signature"):
        verify_qr(build_qr(other_private, _payload(now)), public, now)


def test_editing_the_passenger_count_breaks_the_signature(keys) -> None:
    """Boarding four people on a one-passenger code is the cheapest attack
    available, so the count is inside the signed bytes rather than alongside."""
    private, public = keys
    now = time.time()
    code = build_qr(private, _payload(now, passengers=1))

    ticket_id, _count, slice_, nonce, signature = code.split(".")
    tampered = ".".join((ticket_id, "9", slice_, nonce, signature))

    with pytest.raises(InvalidQr, match="signature"):
        verify_qr(tampered, public, now)


def test_editing_the_ticket_id_breaks_the_signature(keys) -> None:
    private, public = keys
    now = time.time()
    code = build_qr(private, _payload(now))

    _id, count, slice_, nonce, signature = code.split(".")
    tampered = ".".join(("00000000-0000-0000-0000-000000000000", count, slice_, nonce, signature))

    with pytest.raises(InvalidQr, match="signature"):
        verify_qr(tampered, public, now)


def test_an_unsigned_or_truncated_code_is_refused(keys) -> None:
    _, public = keys
    now = time.time()

    for junk in ("", "nonsense", f"{TICKET}.1.2", f"{TICKET}.1.2.nonce"):
        with pytest.raises(InvalidQr):
            verify_qr(junk, public, now)


def test_a_zero_or_negative_passenger_count_is_refused() -> None:
    """Otherwise a group boards while the ticket records nothing."""
    for count in ("0", "-3"):
        with pytest.raises(InvalidQr, match="passenger count"):
            parse_qr(f"{TICKET}.{count}.100.nonce.c2ln")


# --- replay over time -------------------------------------------------------


def test_a_stale_code_is_refused(keys) -> None:
    """A screenshot shared with a friend has to stop working.

    Without this the signature never expires and one valid code is a season
    ticket for the whole fleet.
    """
    private, public = keys
    signed_at = time.time()
    code = build_qr(private, _payload(signed_at))

    much_later = signed_at + SLICE_SECONDS * (SLICE_TOLERANCE + 2)

    with pytest.raises(InvalidQr, match="expired"):
        verify_qr(code, public, much_later)


def test_a_code_from_the_near_future_is_accepted(keys) -> None:
    """The student's phone may be slightly ahead of the helper's.

    Rejecting these would fail honest passengers whenever two phones disagree,
    which is most of the time.
    """
    private, public = keys
    signed_at = time.time()
    code = build_qr(private, _payload(signed_at))

    helper_clock_behind = signed_at - SLICE_SECONDS * SLICE_TOLERANCE

    assert verify_qr(code, public, helper_clock_behind).ticket_id == TICKET


def test_clock_skew_within_tolerance_still_boards(keys) -> None:
    private, public = keys
    signed_at = time.time()
    code = build_qr(private, _payload(signed_at))

    assert verify_qr(code, public, signed_at + SLICE_SECONDS * SLICE_TOLERANCE)


def test_the_tolerance_is_not_open_ended() -> None:
    """A generous window is a replay window. One slice either side is 60
    seconds total, which is the most a nonce log can comfortably cover."""
    assert SLICE_TOLERANCE == 1
    assert SLICE_SECONDS == 30


# --- nonce ------------------------------------------------------------------


def test_every_code_carries_a_distinct_nonce() -> None:
    """The nonce is what a device's redemption log keys on, so collisions
    between honest students would surface as false fraud flags."""
    nonces = {new_nonce() for _ in range(500)}
    assert len(nonces) == 500


def test_two_codes_in_the_same_slice_differ(keys) -> None:
    """Re-rendering the QR must not reproduce the same bytes, or the second
    scan of a legitimately re-shown code looks like a replay."""
    private, _ = keys
    now = time.time()

    assert build_qr(private, _payload(now)) != build_qr(private, _payload(now))
