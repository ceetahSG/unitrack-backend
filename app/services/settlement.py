"""Deciding what a gateway response means for an order, in one place.

Three callers reach this: the browser return, the IPN, and the reconciler. They
learn about a payment by different routes and at different times, but they must
reach the same conclusion about the same money — so the rules live here rather
than being written out three times and drifting apart.

The caller owns the transaction. These functions mutate the ORM objects and
leave committing to whoever called, because the reconciler settles many orders
in a loop and the request handlers settle exactly one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sslcommerz import amount_matches, payment_succeeded, risk_flagged
from app.models.commerce import Order, OrderStatus, Ticket, TicketProduct, TicketStatus
from app.services.boarding import generate_keypair

logger = logging.getLogger("unitrack.settlement")

# Outcomes. Strings rather than an enum because they are a report of what was
# decided, not persisted state — `orders.status` is the state.
PAID = "paid"
FAILED = "failed"
AMOUNT_MISMATCH = "amount_mismatch"
UNDER_REVIEW = "under_review"


async def issue_ticket(db: AsyncSession, order: Order) -> None:
    """Create the ticket for a paid order. At most one, ever.

    `tickets.order_id` is unique, so a second call for the same order raises
    IntegrityError on flush rather than quietly minting a duplicate. Callers
    treat that error as success — the ticket exists, which is the point.
    """
    product = await db.get(TicketProduct, order.product_id)
    if product is None:
        raise RuntimeError(f"order {order.id} references missing product {order.product_id}")

    now = datetime.now(UTC)
    # Per-ticket Ed25519 keypair for the rotating boarding QR (spec §7.2).
    # Asymmetric rather than the HMAC the spec's formula describes: a helper
    # only ever needs to *verify*, so their offline manifest carries public keys
    # and a stolen helper phone cannot forge a code. See app/models/commerce.py.
    private_key, public_key = generate_keypair()
    db.add(
        Ticket(
            order_id=order.id,
            student_id=order.student_id,
            product_id=product.id,
            rides_total=product.ride_count,
            rides_remaining=product.ride_count,
            valid_from=now,
            valid_to=now + timedelta(days=product.validity_days),
            qr_private_key=private_key,
            qr_public_key=public_key,
            status=TicketStatus.active,
        )
    )


async def apply_validation(
    db: AsyncSession, order: Order, validation: dict[str, Any]
) -> str:
    """Settle `order` according to what the gateway says. Does not commit.

    The order of the checks is the security model:

    1. Did it succeed? A status of anything else is not a payment.
    2. Was it the right money? A genuinely VALID response can be for an amount
       the payer chose. Trusting the status alone sells a 100 BDT ticket for 1.
    3. Is it risky? SSLCommerz asking for review is a chargeback waiting to
       happen, so the order is held rather than settled.
    """
    order.raw_payload = validation
    order.gateway_val_id = validation.get("val_id") or order.gateway_val_id
    order.gateway_bank_tran_id = validation.get("bank_tran_id")
    order.gateway_card_type = validation.get("card_type")

    if not payment_succeeded(validation):
        order.status = OrderStatus.failed
        return FAILED

    if not amount_matches(validation, order.amount_paisa, order.currency):
        logger.error(
            "amount mismatch on order %s: expected %s %s, gateway settled %s %s",
            order.id,
            order.amount_paisa,
            order.currency,
            validation.get("currency_amount"),
            validation.get("currency_type"),
        )
        order.status = OrderStatus.failed
        return AMOUNT_MISMATCH

    if risk_flagged(validation):
        logger.warning("order %s flagged for risk review", order.id)
        order.status = OrderStatus.pending
        return UNDER_REVIEW

    order.status = OrderStatus.paid
    order.paid_at = datetime.now(UTC)
    await issue_ticket(db, order)
    return PAID
