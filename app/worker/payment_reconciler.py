"""Payment reconciler (spec §9) — the job that finds money nobody told us about.

An order settles when someone reports the payment: the student's browser coming
back, or SSLCommerz's IPN. Both can fail to arrive. The tab gets closed on a
train, the phone dies, the IPN lands while the API is being redeployed. When
neither shows up the student has paid and holds nothing, and no amount of
retrying on their part fixes it — there is no request left to make.

So this asks the other way round. For every order still unsettled, it queries
SSLCommerz using the `tran_id` we generated ourselves, which needs nothing but
our own records. If the gateway says the money moved, the ticket is issued now,
late but correct.

Two thresholds shape it:

- `GRACE_S` — how long to leave a fresh order alone. A student takes a minute
  or two on the gateway's page, and an order queried mid-payment reports
  nothing useful.
- `ABANDON_H` — when to stop asking. An order the gateway has never heard of
  after this long was never paid; the student opened checkout and walked away.
  Leaving those `pending` forever would bury the real discrepancies.

The dangerous direction here is issuing a ticket that was not paid for, so
every settlement goes through the same `apply_validation` the request handlers
use — including the amount check. A reconciler that trusted the gateway's
status alone would be a way to buy a monthly pass for one taka.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.sslcommerz import GatewayError, SslCommerzClient, payment_succeeded
from app.db.session import SessionLocal
from app.models.commerce import Order, OrderStatus
from app.services.settlement import AMOUNT_MISMATCH, PAID, apply_validation

logger = logging.getLogger("unitrack.worker.reconciler")

INTERVAL_S = 15 * 60
GRACE_S = 10 * 60
ABANDON_H = 24
# One gateway call per order, so a backlog is not turned into a burst.
BATCH = 50


async def _unsettled(db, cutoff: datetime) -> list[Order]:
    stmt = (
        select(Order)
        .where(
            Order.status.in_((OrderStatus.initiated, OrderStatus.pending)),
            Order.created_at < cutoff,
        )
        .order_by(Order.created_at)
        .limit(BATCH)
    )
    return list((await db.execute(stmt)).scalars())


def _successful_element(elements: list[dict]) -> dict | None:
    """The element that represents money actually taken.

    A transaction can carry several attempts — a failed card followed by a
    successful wallet. Only a successful one settles the order, and picking the
    first element blindly would let a failed attempt look authoritative.
    """
    return next((e for e in elements if payment_succeeded(e)), None)


async def reconcile_once(gateway: SslCommerzClient) -> dict[str, int]:
    """One pass. Returns a tally, which is what makes the logs worth reading."""
    tally = {"checked": 0, "settled": 0, "abandoned": 0, "mismatch": 0, "still_open": 0}
    now = datetime.now(UTC)

    async with SessionLocal() as db:
        orders = await _unsettled(db, now - timedelta(seconds=GRACE_S))

        for order in orders:
            tally["checked"] += 1
            try:
                elements = await gateway.query_by_tran_id(order.tran_id)
            except GatewayError as exc:
                # The gateway is unreachable, not the order unpaid. Leave it and
                # try next pass; marking it failed here could destroy a ticket
                # someone paid for.
                logger.warning("reconciler could not query %s: %s", order.tran_id, exc)
                tally["still_open"] += 1
                continue

            paid = _successful_element(elements)

            if paid is None:
                age_h = (now - order.created_at).total_seconds() / 3600
                if age_h >= ABANDON_H:
                    # The gateway has no record of money for this. Abandoned
                    # checkout, not a lost payment.
                    order.status = OrderStatus.failed
                    tally["abandoned"] += 1
                    logger.info("abandoning order %s after %.0fh unpaid", order.id, age_h)
                else:
                    tally["still_open"] += 1
                continue

            outcome = await apply_validation(db, order, paid)
            if outcome == PAID:
                tally["settled"] += 1
                logger.warning(
                    "reconciled order %s: paid at the gateway but never reported — "
                    "ticket issued late",
                    order.id,
                )
            elif outcome == AMOUNT_MISMATCH:
                tally["mismatch"] += 1
                logger.error("order %s settled for the wrong amount — needs a human", order.id)

        try:
            await db.commit()
        except IntegrityError:
            # A ticket appeared between the query and the commit, which means a
            # late IPN won the race. Nothing to fix.
            await db.rollback()
            logger.info("reconciler lost a race to a late report; ticket already exists")

    return tally


async def run() -> None:
    gateway = SslCommerzClient()
    logger.info("payment reconciler running every %ds", INTERVAL_S)
    while True:
        try:
            tally = await reconcile_once(gateway)
            if tally["checked"]:
                logger.info("reconciler pass: %s", tally)
        except Exception:  # noqa: BLE001
            # A reconciler that dies is a reconciler that stops finding lost
            # money, and nothing else notices it is gone.
            logger.exception("reconciler pass failed; continuing")
        await asyncio.sleep(INTERVAL_S)
