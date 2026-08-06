"""Boarding: QR material for students, the manifest and redemptions for helpers.

Three endpoints, one per side of a boarding (spec §7.2, §7.5):

    GET  /shop/tickets/{id}/qr-material   student's device learns to sign
    GET  /helper/manifest                 helper's device learns to verify
    POST /helper/redemptions              helper reports what it accepted

The asymmetry is the security model. A student receives a **private** key and
can therefore only ever produce codes for tickets they own. A helper receives
**public** keys and can therefore only verify, never forge — so a stolen helper
phone, which carries the whole fleet's manifest, is worth nothing.
"""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_approved_helper, require_student
from app.core.authz import Principal
from app.db.session import get_db
from app.models.commerce import Ticket, TicketStatus
from app.models.user import Student, User
from app.schemas.commerce import (
    ManifestTicketOut,
    QrMaterialOut,
    RedemptionBatchIn,
    RedemptionBatchOut,
    RedemptionResultOut,
)
from app.services.boarding import SLICE_SECONDS, parse_qr
from app.services.redemption import RedemptionRejected, redeem

logger = logging.getLogger("unitrack.boarding")

router = APIRouter(tags=["boarding"])


# ---------------------------------------------------------------------------
# Student side
# ---------------------------------------------------------------------------


@router.get("/shop/tickets/{ticket_id}/qr-material", response_model=QrMaterialOut)
async def qr_material(
    ticket_id: uuid.UUID,
    principal: Principal = Depends(require_student),
    db: AsyncSession = Depends(get_db),
) -> QrMaterialOut:
    """Hand a student's device what it needs to generate boarding codes offline.

    Scoped to the caller's own ticket — the id in the path is checked against
    the caller's student row, never trusted on its own. Without that, any
    student could ask for any ticket's private key by guessing a uuid, which
    would hand them the whole system.
    """
    student = (
        await db.execute(select(Student).where(Student.user_id == principal.user_id))
    ).scalar_one_or_none()
    if student is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Account has no student profile")

    ticket = await db.get(Ticket, ticket_id)
    if ticket is None or ticket.student_id != student.id:
        # Same answer for "does not exist" and "belongs to someone else", so
        # this cannot be used to discover which ticket ids are real.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")

    if ticket.status is not TicketStatus.active:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Ticket is {ticket.status}")

    return QrMaterialOut(
        ticket_id=ticket.id,
        qr_private_key=ticket.qr_private_key,
        slice_seconds=SLICE_SECONDS,
        # The device stores (server_time - device_time) and applies it to every
        # slice it computes, so a wrong phone clock does not silently produce
        # codes that are rejected at the door.
        server_time=datetime.now(UTC),
        passenger_count=1,
        valid_to=ticket.valid_to,
    )


# ---------------------------------------------------------------------------
# Helper side
# ---------------------------------------------------------------------------


@router.get("/helper/manifest", response_model=list[ManifestTicketOut])
async def ticket_manifest(
    db: AsyncSession = Depends(get_db),
    _: Principal = Depends(require_approved_helper),
    limit: int = Query(default=2000, ge=1, le=10000),
) -> list[ManifestTicketOut]:
    """The offline ticket manifest (spec §7.5).

    Synced at trip start on whatever signal is available. It carries public
    keys only, plus enough identity for the dead-phone fallback: a student
    whose battery died gives their student ID and the helper finds them here.

    Only active tickets are listed, which is also how revocation reaches an
    offline helper — a revoked ticket simply stops appearing, and the staleness
    window is the time since their last sync.
    """
    now = datetime.now(UTC)
    # Explicitly joined rather than walked through relationships: this runs once
    # per trip start for every active ticket in the fleet, and a lazy load per
    # row would turn one query into thousands.
    stmt = (
        select(
            Ticket.id,
            Ticket.qr_public_key,
            Ticket.rides_remaining,
            Ticket.valid_to,
            Ticket.status,
            User.name,
            Student.student_id_no,
        )
        .join(Student, Student.id == Ticket.student_id)
        .join(User, User.id == Student.user_id)
        .where(Ticket.status == TicketStatus.active, Ticket.valid_to >= now)
        .order_by(Ticket.valid_to)
        .limit(limit)
    )

    return [
        ManifestTicketOut(
            ticket_id=row.id,
            qr_public_key=row.qr_public_key,
            student_name=row.name,
            student_id_no=row.student_id_no,
            rides_remaining=row.rides_remaining,
            valid_to=row.valid_to,
            status=row.status,
        )
        for row in (await db.execute(stmt)).all()
    ]


@router.post("/helper/redemptions", response_model=RedemptionBatchOut)
async def sync_redemptions(
    payload: RedemptionBatchIn,
    principal: Principal = Depends(require_approved_helper),
    db: AsyncSession = Depends(get_db),
) -> RedemptionBatchOut:
    """Record boardings a helper's device accepted, online or hours ago.

    Every item is answered individually. One forged code in a batch of forty
    must not cost the other thirty-nine — a helper who loses a route's worth of
    genuine boardings because a single scan was bad has lost real revenue.

    Each result is committed on its own for the same reason: a later item
    failing must not roll back earlier valid ones.
    """
    results: list[RedemptionResultOut] = []

    for item in payload.redemptions:
        nonce = None
        try:
            nonce = parse_qr(item.code)[0].nonce
        except Exception:  # noqa: BLE001 - a malformed code still needs an answer
            pass

        try:
            redemption = await redeem(
                db,
                code=item.code,
                helper_id=principal.helper_id,
                device_id=item.device_id,
                redeemed_at=item.redeemed_at,
                trip_id=item.trip_id,
            )
            await db.flush()
            ticket = await db.get(Ticket, redemption.ticket_id)
            await db.commit()

            results.append(
                RedemptionResultOut(
                    nonce=redemption.nonce,
                    accepted=True,
                    reason="recorded",
                    ticket_id=redemption.ticket_id,
                    rides_remaining=ticket.rides_remaining if ticket else None,
                    flag=redemption.flag,
                )
            )
        except RedemptionRejected as exc:
            await db.rollback()
            results.append(
                # Accepted False, but the device still drops it: retrying a
                # forged or expired code forever is a queue that never drains.
                RedemptionResultOut(nonce=nonce, accepted=False, reason=str(exc))
            )
        except IntegrityError:
            # This device already reported this nonce and the row landed
            # between our check and the insert. Its ride is counted.
            await db.rollback()
            results.append(
                RedemptionResultOut(nonce=nonce, accepted=True, reason="already recorded")
            )

    return RedemptionBatchOut(results=results)
