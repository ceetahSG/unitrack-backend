"""Fraud sweep (spec §7.2 step 4, §7.5) — acting on what offline boarding lets through.

Offline validation has one hole that cryptography cannot close. Two helper
phones with no signal cannot see each other's nonce logs, so the same boarding
code can be accepted at two doors before either syncs. The signature is genuine,
the time slice is fresh, and both devices were right to accept it.

Detection is therefore the whole defence, and it is why `redeem` records a
second sighting as `duplicate_suspect` rather than refusing the sync — a refused
sync destroys the only evidence that it happened.

This job is the consequence half. On its own, a flag in a table changes nothing:
the ticket keeps working and nobody is told. Here the ticket is suspended and an
alert is raised, which is what makes the accepted trade-off in §7.5 an actual
trade rather than a hole.

**Suspend, not revoke.** A duplicate is not proof of fraud — a student can hand
their phone to a friend, but a helper can also scan the same screen twice
because the first attempt looked like it failed. Suspension stops further
boarding and puts a human in the loop; revocation would destroy a paid ticket on
a suspicion.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionLocal
from app.models.commerce import Redemption, Ticket, TicketStatus
from app.models.ops import Alert, AlertSeverity, AlertSource, AlertStatus, AlertType

logger = logging.getLogger("unitrack.worker.fraud")

INTERVAL_S = 10 * 60

# One ticket, one code, accepted on more than this many distinct devices.
# Two is the threshold because two is already impossible honestly: a single
# code scanned at two different doors means either a shared screen or a
# duplicated device log.
DEVICE_THRESHOLD = 2

BATCH = 100


async def _unhandled_duplicates(db: AsyncSession) -> list[tuple]:
    """Nonces accepted on several devices, whose ticket is still active.

    Counted across **every** sighting of the nonce, not only the ones flagged
    `duplicate_suspect`. The first device to sync is recorded as `ok` — it did
    nothing wrong and could not have known — so only the later sightings carry
    the flag. Filtering on it counts one device per nonce and the threshold is
    never reached, which is exactly the bug this comment exists to prevent.

    Filtering on the *ticket's* status is what makes the sweep idempotent: once
    a ticket is suspended it stops matching, so the same evidence is not
    re-alerted every ten minutes for the rest of time.
    """
    stmt = (
        select(
            Redemption.nonce,
            Redemption.ticket_id,
            func.count(func.distinct(Redemption.helper_device_id)).label("devices"),
        )
        .join(Ticket, Ticket.id == Redemption.ticket_id)
        .where(Ticket.status == TicketStatus.active)
        .group_by(Redemption.nonce, Redemption.ticket_id)
        .having(func.count(func.distinct(Redemption.helper_device_id)) >= DEVICE_THRESHOLD)
        .limit(BATCH)
    )
    return list((await db.execute(stmt)).all())


async def sweep_once() -> dict[str, int]:
    """One pass. Returns a tally, which is what makes the log worth reading."""
    tally = {"flagged": 0, "suspended": 0}

    async with SessionLocal() as db:
        rows = await _unhandled_duplicates(db)

        for nonce, ticket_id, devices in rows:
            tally["flagged"] += 1
            ticket = await db.get(Ticket, ticket_id)
            if ticket is None or ticket.status is not TicketStatus.active:
                continue

            # `suspended`, not `revoked`: see the module docstring. It stops
            # boarding while leaving the ticket restorable by an admin who
            # decides it was a double scan rather than a shared screen.
            ticket.status = TicketStatus.suspended
            tally["suspended"] += 1

            db.add(
                Alert(
                    source=AlertSource.system,
                    type=AlertType.other,
                    severity=AlertSeverity.warning,
                    message=(
                        f"Boarding code reused on {devices} devices for ticket "
                        f"{ticket_id}. Ticket suspended pending review."
                    ),
                    status=AlertStatus.open,
                )
            )
            logger.warning(
                "ticket %s suspended: nonce %s accepted on %d devices",
                ticket_id,
                nonce,
                devices,
            )

        if tally["suspended"]:
            await db.commit()

    return tally


async def run() -> None:
    logger.info("fraud sweep running every %ds", INTERVAL_S)
    while True:
        try:
            tally = await sweep_once()
            if tally["flagged"]:
                logger.info("fraud sweep: %s", tally)
        except Exception:  # noqa: BLE001
            # A sweep that dies stops enforcing the only consequence offline
            # duplication has, and nothing else would notice it was gone.
            logger.exception("fraud sweep failed; continuing")
        await asyncio.sleep(INTERVAL_S)
