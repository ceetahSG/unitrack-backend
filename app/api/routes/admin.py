"""Admin routes — and the worked example of how to secure a route in this API.

If you are adding endpoints, copy the shape of this file. The four rules it
demonstrates, in order of how easy they are to get wrong:

1. Guard the **router**, not each route (see below).
2. Take `Principal`, not `User`, unless you need profile columns.
3. Call `invalidate_principal()` after every write to `users` / `helpers`.
4. Register the path in `PUBLIC_PATHS` only if it is genuinely public — the
   coverage test in `tests/test_auth_coverage.py` will fail the build for any
   route that is neither guarded nor explicitly listed there.

Replaces the `scripts/dev_seed_fleet.py` approval shortcut, whose own docstring
called itself out: "the real path is an admin approval endpoint".
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.authz import Principal, invalidate_principal
from app.core.redis import get_redis
from app.db.session import get_db
from app.models.fleet import Bus
from app.models.ops import Alert, AlertStatus
from app.models.user import Helper, HelperStatus, User, UserStatus
from app.schemas.admin import HelperOut
from app.schemas.fleet import BusCreate, BusListCreate, BusOut
from app.schemas.ops import AlertOut, AlertResolveIn

# ---------------------------------------------------------------------------
# RULE 1 — the guard lives on the router.
#
# `dependencies=[Depends(require_admin)]` applies to every route declared on
# this router, including ones added later by someone who never read this
# comment. That is the point: security by construction, not by remembering.
#
# FastAPI resolves it before the handler body runs, so a non-admin never
# reaches your code. It also marks all these routes as authenticated in
# /docs and openapi.json, so generated clients know to send the header.
# ---------------------------------------------------------------------------
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
    responses={
        401: {"description": "Missing, malformed or expired access token"},
        403: {"description": "Authenticated, but not an admin"},
    },
)


def _to_out(user: User, helper: Helper) -> HelperOut:
    return HelperOut(
        helper_id=helper.id,
        user_id=user.id,
        name=user.name,
        email=user.email,
        phone=user.phone,
        helper_status=helper.status,
        user_status=user.status,
        approved_by=helper.approved_by,
    )


@router.get("/helpers", response_model=list[HelperOut])
async def list_helpers(
    db: AsyncSession = Depends(get_db),
    helper_status: HelperStatus | None = Query(
        default=None, description="Filter by approval state; omit for all."
    ),
) -> list[HelperOut]:
    """List helper accounts — the admin panel's approval queue.

    Note there is no auth code in this handler. The router guard already ran;
    by the time we are here the caller is a known, active admin.
    """
    stmt = select(User, Helper).join(Helper, Helper.user_id == User.id)
    if helper_status is not None:
        stmt = stmt.where(Helper.status == helper_status)
    rows = (await db.execute(stmt.order_by(User.created_at))).all()
    return [_to_out(user, helper) for user, helper in rows]


@router.post("/helpers/{helper_id}/approve", response_model=HelperOut)
async def approve_helper(
    helper_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
    # ---------------------------------------------------------------------
    # RULE 2 — ask for `Principal`, not `User`.
    #
    # The router guard already resolved it, so this parameter is *free*:
    # FastAPI caches dependency results per request and hands back the same
    # object. Declaring `get_current_user` here instead would cost a second
    # SELECT purely to learn an id we already have.
    #
    # We need it because approval is an audited action — `approved_by`
    # records which admin did it.
    # ---------------------------------------------------------------------
    admin: Principal = Depends(require_admin),
) -> HelperOut:
    """Approve a pending helper so they can start sending GPS (spec §8)."""
    helper = await db.get(Helper, helper_id)
    if helper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown helper")
    if helper.status is HelperStatus.approved:
        raise HTTPException(status.HTTP_409_CONFLICT, "Helper is already approved")

    user = await db.get(User, helper.user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Helper has no user row")

    helper.status = HelperStatus.approved
    helper.approved_by = admin.user_id
    user.status = UserStatus.active
    await db.commit()

    # -----------------------------------------------------------------------
    # RULE 3 — invalidate after the commit.
    #
    # This helper's cached Principal still says `pending`. Without this line
    # they would keep getting 403 from POST /helper/gps for up to
    # PRINCIPAL_TTL_S (5 minutes) after being approved — and the mirror-image
    # bug is far worse: a *suspended* account that keeps working for 5 minutes.
    #
    # After the commit, not before: invalidating first leaves a window where a
    # concurrent request re-populates the cache from the pre-commit state.
    # -----------------------------------------------------------------------
    await invalidate_principal(r, helper.user_id)

    # No refresh() needed: the session is configured with expire_on_commit=False,
    # so these instances keep their values after the commit.
    return _to_out(user, helper)


@router.post("/users/{user_id}/suspend", status_code=status.HTTP_204_NO_CONTENT)
async def suspend_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
    admin: Principal = Depends(require_admin),
) -> None:
    """Suspend an account. Takes effect on the caller's very next request.

    Immediate revocation is exactly what RULE 3 buys. A suspended user's access
    token is still cryptographically valid and unexpired — nothing can un-issue
    it. What stops them is `get_principal` reading a fresh snapshot that says
    `suspended` and raising 401. Skip the invalidation and that check reads a
    stale snapshot instead, so a suspended account keeps its access.
    """
    if user_id == admin.user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot suspend yourself")

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown user")

    user.status = UserStatus.suspended
    if (helper := (await db.execute(
        select(Helper).where(Helper.user_id == user_id)
    )).scalar_one_or_none()) is not None:
        helper.status = HelperStatus.suspended
    await db.commit()

    await invalidate_principal(r, user_id)


# ---------------------------------------------------------------------------
# Emergency console (spec §7.6)
# ---------------------------------------------------------------------------


@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(
    db: AsyncSession = Depends(get_db),
    alert_status: AlertStatus | None = Query(
        default=AlertStatus.open, description="Defaults to open; pass null for all."
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Alert]:
    """The emergency console's list — worst first, newest first within severity.

    Backed by `ix_alerts_status_severity`, so the default view stays an index
    scan over open rows rather than a sort of the whole table as history grows.
    """
    stmt = select(Alert)
    if alert_status is not None:
        stmt = stmt.where(Alert.status == alert_status)
    stmt = stmt.order_by(Alert.severity, Alert.created_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars())


@router.post("/alerts/{alert_id}/acknowledge", response_model=AlertOut)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(require_admin),
) -> Alert:
    """Claim an alert so two admins do not work the same incident."""
    alert = await db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown alert")
    if alert.status is not AlertStatus.open:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Alert is already {alert.status}")

    alert.status = AlertStatus.acknowledged
    alert.acknowledged_by = admin.user_id
    await db.commit()
    return alert


@router.post("/alerts/{alert_id}/resolve", response_model=AlertOut)
async def resolve_alert(
    alert_id: uuid.UUID,
    body: AlertResolveIn,
    db: AsyncSession = Depends(get_db),
    admin: Principal = Depends(require_admin),
) -> Alert:
    """Close an incident. A resolved alert keeps who acknowledged it and why."""
    alert = await db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown alert")
    if alert.status in (AlertStatus.resolved, AlertStatus.dismissed):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Alert is already {alert.status}")

    alert.status = AlertStatus.resolved
    alert.resolved_note = body.note
    alert.resolved_at = datetime.now(UTC)
    if alert.acknowledged_by is None:
        alert.acknowledged_by = admin.user_id
    await db.commit()
    return alert


# ---------------------------------------------------------------------------
# Fleet management (buses)
# ---------------------------------------------------------------------------


@router.post("/buses", response_model=BusOut, status_code=status.HTTP_201_CREATED)
async def create_bus(
    body: BusCreate,
    db: AsyncSession = Depends(get_db),
) -> Bus:
    """Create a new bus in the fleet."""
    stmt = select(Bus).where(Bus.reg_no == body.reg_no)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Bus with reg_no '{body.reg_no}' already exists",
        )

    bus = Bus(
        reg_no=body.reg_no,
        nickname=body.nickname,
        capacity=body.capacity,
        status=body.status,
    )
    db.add(bus)
    await db.commit()
    return bus


@router.post("/buses/batch", response_model=list[BusOut], status_code=status.HTTP_201_CREATED)
@router.post("/buses/list", response_model=list[BusOut], status_code=status.HTTP_201_CREATED)
async def create_bus_list(
    body: BusListCreate,
    db: AsyncSession = Depends(get_db),
) -> list[Bus]:
    """Create multiple buses at once (batch creation)."""
    if not body.buses:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Bus list cannot be empty")

    seen = set()
    duplicates = set()
    for b in body.buses:
        if b.reg_no in seen:
            duplicates.add(b.reg_no)
        seen.add(b.reg_no)

    if duplicates:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Duplicate reg_no in payload: {', '.join(sorted(duplicates))}",
        )

    stmt = select(Bus.reg_no).where(Bus.reg_no.in_(list(seen)))
    existing_regs = set((await db.execute(stmt)).scalars())
    if existing_regs:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Buses already exist with reg_no: {', '.join(sorted(existing_regs))}",
        )

    new_buses = [
        Bus(
            reg_no=b.reg_no,
            nickname=b.nickname,
            capacity=b.capacity,
            status=b.status,
        )
        for b in body.buses
    ]
    db.add_all(new_buses)
    await db.commit()
    return new_buses

