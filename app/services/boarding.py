"""The rotating boarding QR: what it contains, and what makes one valid.

Spec §7.2. The code a student shows is:

    ticket_id · passenger_count · time_slice · nonce · signature

signed with the ticket's Ed25519 private key. It re-signs every 30 seconds, so
a screenshot passed to a friend is worthless within half a minute.

Four independent things have to hold for a boarding to be accepted, and each
closes a different attack:

1. **Signature** — proves the code came from a device holding that ticket's
   private key. Without it anyone could type a ticket id into a QR generator.
2. **Time slice** — proves it was made *recently*. Without it a signature,
   once captured, is a season ticket.
3. **Nonce** — proves this particular code has not already been used. Without
   it one valid 30-second window boards a whole queue.
4. **Ticket state** — active, in date, rides remaining.

Verification runs identically on the server and, once the Flutter side lands,
on a helper's phone with no signal — which is the entire point of §7.5. So
nothing here reads the database or the clock beyond what it is handed.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# 30 seconds, per spec §7.2. Short enough that a shared screenshot expires
# before it reaches the door; long enough that a slow scan still lands.
SLICE_SECONDS = 30

# ±1 slice. Phone clocks drift and the student's and helper's rarely agree, so
# a window of exactly one slice would reject honest passengers constantly. Two
# slices of tolerance is 60 seconds of forgiveness, and the nonce log — not the
# window — is what stops replay inside it.
SLICE_TOLERANCE = 1

_FIELD_SEP = "."


class InvalidQr(Exception):
    """The code is malformed, unsigned, forged, or stale."""


@dataclass(frozen=True, slots=True)
class QrPayload:
    ticket_id: str
    passenger_count: int
    time_slice: int
    nonce: str

    def signing_bytes(self) -> bytes:
        """The exact bytes that get signed.

        Order and separator are fixed forever: the student's device, the
        helper's device and the server must all reproduce this byte-for-byte or
        every signature fails. Changing it is a breaking change to every ticket
        already in circulation.
        """
        return _FIELD_SEP.join(
            (self.ticket_id, str(self.passenger_count), str(self.time_slice), self.nonce)
        ).encode()


def generate_keypair() -> tuple[str, str]:
    """A ticket's (private, public) keys, raw and base64url encoded."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return _b64(private), _b64(public)


def current_slice(unix_seconds: float) -> int:
    return int(unix_seconds // SLICE_SECONDS)


def new_nonce() -> str:
    """16 bytes. Guessing one is not an attack worth defending against — the
    signature already gates that — but collisions between honest devices would
    show up as false duplicate-suspect flags, so it is generously sized."""
    return secrets.token_urlsafe(16)


def build_qr(private_key_b64: str, payload: QrPayload) -> str:
    """The string a student's device renders as a QR code."""
    key = Ed25519PrivateKey.from_private_bytes(_unb64(private_key_b64))
    signature = key.sign(payload.signing_bytes())
    return _FIELD_SEP.join(
        (
            payload.ticket_id,
            str(payload.passenger_count),
            str(payload.time_slice),
            payload.nonce,
            _b64(signature),
        )
    )


def parse_qr(code: str) -> tuple[QrPayload, bytes]:
    """Split a scanned code. Raises `InvalidQr` rather than returning None, so
    a caller cannot forget to check."""
    parts = code.split(_FIELD_SEP)
    if len(parts) != 5:
        raise InvalidQr("malformed code")

    ticket_id, raw_count, raw_slice, nonce, raw_signature = parts
    try:
        payload = QrPayload(
            ticket_id=ticket_id,
            passenger_count=int(raw_count),
            time_slice=int(raw_slice),
            nonce=nonce,
        )
        signature = _unb64(raw_signature)
    except (ValueError, TypeError) as exc:
        raise InvalidQr("malformed code") from exc

    if payload.passenger_count < 1:
        # A zero or negative count would let someone board a group while
        # recording nothing against the ticket.
        raise InvalidQr("passenger count must be at least 1")

    return payload, signature


def verify_qr(code: str, public_key_b64: str, now_unix: float) -> QrPayload:
    """Check a scanned code against a ticket's public key and the clock.

    Signature first, then freshness. The order matters only for clarity — both
    must pass — but checking the signature first means a malformed or forged
    code never reaches the time comparison.

    Does **not** check the nonce or the ticket's state: neither is knowable from
    the code alone. The caller owns those, because offline on a helper's phone
    they come from a local SQLite log and a synced manifest, while on the server
    they come from Postgres.
    """
    payload, signature = parse_qr(code)

    try:
        Ed25519PublicKey.from_public_bytes(_unb64(public_key_b64)).verify(
            signature, payload.signing_bytes()
        )
    except (InvalidSignature, ValueError) as exc:
        raise InvalidQr("signature does not match this ticket") from exc

    drift = abs(current_slice(now_unix) - payload.time_slice)
    if drift > SLICE_TOLERANCE:
        raise InvalidQr("code has expired")

    return payload


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode())
