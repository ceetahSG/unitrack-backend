"""What an operator configures before the system can run at all.

`admin.py` is the day-to-day console — approve a helper, acknowledge an alert.
This is the setup underneath it: what is for sale, where the buses stop, and
which order they stop in. Until now none of it had an endpoint. Products were
created by hand-written SQL and routes by `scripts/dev_seed_routes.py`, which
meant launching a route or changing a fare required a shell on the production
box. That is not an operations model; it is a person with psql.

Two rules run through the whole file, and both come from the foreign keys:

**Nothing here deletes what money or history points at.** `orders`, `tickets`
and `trips` reference products and routes with `ON DELETE RESTRICT`, on purpose
— a paid ticket must still name what was bought a year later. So retiring a
product or a route is `active = false`, not a DELETE. The row stops appearing
to students and keeps answering for the past.

**A stop is deletable only while nothing uses it.** `route_stops` restricts it,
so the database refuses and this turns that into a 409 rather than a 500.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_admin
from app.db.session import get_db
from app.models.commerce import TicketProduct
from app.models.fleet import Route, RouteStop, Stop
from app.schemas.commerce import AdminProductOut, ProductCreate, ProductUpdate
from app.schemas.fleet import (
    RouteCreate,
    RouteDetailOut,
    RouteOut,
    RouteStopOut,
    RouteStopsReplace,
    RouteUpdate,
    StopCreate,
    StopOut,
    StopUpdate,
)

# Guarded at the router, so every route below inherits it — including the next
# one somebody adds. See app/api/routes/admin.py for why this is the only place
# the check belongs.
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
    responses={
        401: {"description": "Missing, malformed or expired access token"},
        403: {"description": "Authenticated, but not an admin"},
    },
)


# ---------------------------------------------------------------------------
# Ticket products — the catalogue
# ---------------------------------------------------------------------------


@router.get("/products", response_model=list[AdminProductOut])
async def list_products(
    db: AsyncSession = Depends(get_db),
    include_inactive: bool = Query(default=True, description="Withdrawn products too."),
) -> list[TicketProduct]:
    """The full catalogue, withdrawn products included.

    Differs from `GET /shop/products` deliberately: students see only what they
    can buy, while an operator has to see what they retired last month in order
    to bring it back.
    """
    stmt = select(TicketProduct)
    if not include_inactive:
        stmt = stmt.where(TicketProduct.active.is_(True))
    return list((await db.execute(stmt.order_by(TicketProduct.price_paisa))).scalars())


@router.post("/products", response_model=AdminProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(
    body: ProductCreate,
    db: AsyncSession = Depends(get_db),
) -> TicketProduct:
    """Put something on sale."""
    if body.route_scope is not None and await db.get(Route, body.route_scope) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown route for route_scope")

    product = TicketProduct(**body.model_dump())
    db.add(product)
    await db.commit()
    return product


@router.patch("/products/{product_id}", response_model=AdminProductOut)
async def update_product(
    product_id: uuid.UUID,
    body: ProductUpdate,
    db: AsyncSession = Depends(get_db),
) -> TicketProduct:
    """Edit a product, or withdraw it with `active: false`.

    `exclude_unset=True` is what separates "clear the route scope" from "leave
    the route scope alone" — both arrive as `route_scope: null` otherwise, and
    every edit to a price would silently unscope the product.
    """
    product = await db.get(TicketProduct, product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown product")

    changes = body.model_dump(exclude_unset=True)
    scope = changes.get("route_scope")
    if scope is not None and await db.get(Route, scope) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown route for route_scope")

    for field, value in changes.items():
        setattr(product, field, value)
    await db.commit()
    return product


# ---------------------------------------------------------------------------
# Stops
# ---------------------------------------------------------------------------


@router.post("/stops", response_model=StopOut, status_code=status.HTTP_201_CREATED)
async def create_stop(body: StopCreate, db: AsyncSession = Depends(get_db)) -> Stop:
    stop = Stop(**body.model_dump())
    db.add(stop)
    await db.commit()
    return stop


@router.patch("/stops/{stop_id}", response_model=StopOut)
async def update_stop(
    stop_id: uuid.UUID,
    body: StopUpdate,
    db: AsyncSession = Depends(get_db),
) -> Stop:
    stop = await db.get(Stop, stop_id)
    if stop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown stop")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(stop, field, value)
    await db.commit()
    return stop


@router.delete("/stops/{stop_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stop(stop_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    """Remove a stop that no route uses.

    Checked before attempting the delete rather than catching the database's
    refusal, so the answer can say *which* routes are in the way. An operator
    who is told "in use by 2 routes" knows what to do next; one who is told
    "integrity error" does not.
    """
    stop = await db.get(Stop, stop_id)
    if stop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown stop")

    in_use = len(
        (await db.execute(select(RouteStop.route_id).where(RouteStop.stop_id == stop_id))).all()
    )
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Stop is used by {in_use} route(s); remove it from them first",
        )

    await db.delete(stop)
    await db.commit()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _route_detail(db: AsyncSession, route_id: uuid.UUID) -> RouteDetailOut:
    """One route with its ordered stops, loaded without an N+1 storm.

    `populate_existing` is load-bearing, not tidiness. The session runs with
    `expire_on_commit=False`, and the replace below deletes rows with a bulk
    `DELETE` that the identity map never sees. So a `Route` already loaded in
    this request keeps its old `stops` collection, and re-querying hands back
    that same stale object — a reorder would answer with the previous order and
    look, to whoever just saved it, like the change had not stuck.
    """
    stmt = (
        select(Route)
        .where(Route.id == route_id)
        .options(selectinload(Route.stops).selectinload(RouteStop.stop))
        .execution_options(populate_existing=True)
    )
    route = (await db.execute(stmt)).scalar_one_or_none()
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown route")

    return RouteDetailOut(
        id=route.id,
        name=route.name,
        direction=route.direction,
        is_active=route.is_active,
        polyline=route.polyline,
        stops=[
            RouteStopOut(
                seq=rs.seq,
                scheduled_offset_min=rs.scheduled_offset_min,
                stop=StopOut.model_validate(rs.stop),
            )
            for rs in route.stops
        ],
    )


@router.post("/routes", response_model=RouteOut, status_code=status.HTTP_201_CREATED)
async def create_route(body: RouteCreate, db: AsyncSession = Depends(get_db)) -> Route:
    """Create a route. Stops are attached separately — see `PUT /routes/{id}/stops`."""
    route = Route(**body.model_dump())
    db.add(route)
    try:
        await db.commit()
    except IntegrityError as exc:
        # uq_routes_name_direction. Two routes can share a name only if they run
        # in opposite directions, which is exactly how an out-and-back pair is
        # modelled.
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A {body.direction} route named '{body.name}' already exists",
        ) from exc
    return route


@router.patch("/routes/{route_id}", response_model=RouteOut)
async def update_route(
    route_id: uuid.UUID,
    body: RouteUpdate,
    db: AsyncSession = Depends(get_db),
) -> Route:
    """Edit a route, or retire it with `is_active: false`.

    There is no DELETE. `trips` reference routes with RESTRICT and every
    completed trip is history someone may report on, so retiring is the only
    safe removal — it disappears from `/fleet/routes` while the past still
    resolves.
    """
    route = await db.get(Route, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown route")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(route, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Another route already has that name and direction"
        ) from exc
    return route


@router.put("/routes/{route_id}/stops", response_model=RouteDetailOut)
async def replace_route_stops(
    route_id: uuid.UUID,
    body: RouteStopsReplace,
    db: AsyncSession = Depends(get_db),
) -> RouteDetailOut:
    """Set the route's complete ordered stop list.

    Sequence numbers come from list position, so the client expresses order by
    ordering rather than by numbering — see `RouteStopIn` for why that is not
    just a convenience.

    The old rows are deleted and flushed **before** the new ones are added.
    Without that flush SQLAlchemy is free to emit the inserts first, and
    `uq_route_stops_route_seq` rejects seq 1 while the previous seq 1 is still
    there. Both halves are one transaction, so a bad stop id leaves the existing
    route untouched rather than wiping it.
    """
    if await db.get(Route, route_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown route")

    stop_ids = [s.stop_id for s in body.stops]
    if len(set(stop_ids)) != len(stop_ids):
        # uq_route_stops_route_stop would catch this, but a named error beats an
        # integrity error — and a route that genuinely revisits a stop needs a
        # data model change, not a retry.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A stop is listed more than once")

    known = set(
        (await db.execute(select(Stop.id).where(Stop.id.in_(stop_ids)))).scalars()
    )
    if missing := [str(s) for s in stop_ids if s not in known]:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Unknown stop(s): {', '.join(missing)}"
        )

    await db.execute(delete(RouteStop).where(RouteStop.route_id == route_id))
    await db.flush()

    db.add_all(
        [
            RouteStop(
                route_id=route_id,
                stop_id=item.stop_id,
                seq=position,
                scheduled_offset_min=item.scheduled_offset_min,
            )
            for position, item in enumerate(body.stops, start=1)
        ]
    )
    await db.commit()

    return await _route_detail(db, route_id)
