"""Operating the catalogue and the route network over HTTP.

These endpoints exist because the alternative was psql on the production box.
The tests worth writing are therefore not "does POST create a row" but the
cases where a careless implementation quietly destroys something:

- a PATCH that unscopes a product nobody asked to unscope,
- a stop deleted out from under a route,
- a reorder that half-applies and leaves the route in neither order.

Each one below is a way an operator loses data by doing something reasonable.
"""

import uuid

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_db, get_principal
from app.core.authz import Principal
from app.db.base import Base
from app.main import create_app
from app.models.fleet import Route, RouteDirection, RouteStop, Stop
from app.models.user import UserRole, UserStatus


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def client_app(db):
    app = create_app()

    async def _db():
        yield db

    async def _principal():
        return Principal(
            user_id=uuid.uuid4(), role=UserRole.admin, status=UserStatus.active
        )

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_principal] = _principal
    return app


@pytest.fixture
async def client(client_app):
    async with AsyncClient(
        transport=ASGITransport(app=client_app), base_url="http://test"
    ) as c:
        yield c


async def _make_stops(db: AsyncSession, count: int) -> list[Stop]:
    stops = [
        Stop(name=f"Stop {i}", lat=23.74 + i / 100, lng=90.37 + i / 100)
        for i in range(count)
    ]
    db.add_all(stops)
    await db.commit()
    return stops


async def _make_route(db: AsyncSession, name: str = "Dhanmondi") -> Route:
    route = Route(name=name, direction=RouteDirection.inbound)
    db.add(route)
    await db.commit()
    return route


# --- products --------------------------------------------------------------


async def test_a_product_can_be_created_and_appears_in_the_catalogue(client) -> None:
    created = await client.post(
        "/admin/products",
        json={"type": "single", "name": "Single ride", "price_paisa": 3000},
    )
    assert created.status_code == status.HTTP_201_CREATED
    assert created.json()["price_paisa"] == 3000
    assert created.json()["active"] is True

    listed = await client.get("/admin/products")
    assert [p["name"] for p in listed.json()] == ["Single ride"]


async def test_withdrawing_a_product_hides_it_from_students_only(client) -> None:
    """Retiring is `active: false`, never a DELETE.

    `orders` and `tickets` reference products with RESTRICT, because a ticket
    sold last term must still name what was bought. So the row survives and
    simply stops being offered.
    """
    product_id = (
        await client.post(
            "/admin/products",
            json={"type": "single", "name": "Old fare", "price_paisa": 2000},
        )
    ).json()["id"]

    patched = await client.patch(f"/admin/products/{product_id}", json={"active": False})
    assert patched.status_code == status.HTTP_200_OK
    assert patched.json()["active"] is False

    # Still visible to an operator, so it can be brought back.
    assert len((await client.get("/admin/products")).json()) == 1
    assert (await client.get("/admin/products?include_inactive=false")).json() == []


async def test_editing_a_price_does_not_clear_the_route_scope(client, db) -> None:
    """The bug `exclude_unset` exists to prevent.

    `route_scope` is nullable, so an unset field and an explicit null arrive
    identically unless the handler distinguishes them. Without that, every
    routine price change would silently make a route-locked pass valid
    everywhere — and nothing would look wrong until someone rode for free.
    """
    route = await _make_route(db)
    product_id = (
        await client.post(
            "/admin/products",
            json={
                "type": "package",
                "name": "Dhanmondi monthly",
                "price_paisa": 90000,
                "route_scope": str(route.id),
            },
        )
    ).json()["id"]

    patched = await client.patch(f"/admin/products/{product_id}", json={"price_paisa": 95000})
    assert patched.status_code == status.HTTP_200_OK
    assert patched.json()["price_paisa"] == 95000
    assert patched.json()["route_scope"] == str(route.id)


async def test_a_route_scope_can_still_be_cleared_deliberately(client, db) -> None:
    """The other half: an explicit null must actually clear it."""
    route = await _make_route(db)
    product_id = (
        await client.post(
            "/admin/products",
            json={
                "type": "package",
                "name": "Scoped",
                "price_paisa": 5000,
                "route_scope": str(route.id),
            },
        )
    ).json()["id"]

    patched = await client.patch(f"/admin/products/{product_id}", json={"route_scope": None})
    assert patched.json()["route_scope"] is None


async def test_a_product_cannot_be_scoped_to_a_route_that_does_not_exist(client) -> None:
    response = await client.post(
        "/admin/products",
        json={
            "type": "package",
            "name": "Ghost route pass",
            "price_paisa": 5000,
            "route_scope": str(uuid.uuid4()),
        },
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_a_zero_ride_product_is_refused(client) -> None:
    """`ride_count: 0` is a ticket that can never be used. Null means unlimited,
    which is why the floor is 1 rather than 0."""
    response = await client.post(
        "/admin/products",
        json={"type": "bulk", "name": "Nothing", "price_paisa": 100, "ride_count": 0},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


# --- stops -----------------------------------------------------------------


async def test_a_stop_in_use_cannot_be_deleted(client, db) -> None:
    """The database would refuse anyway; this turns that into an answer.

    An operator told "used by 1 route(s)" knows what to do next. One told
    "IntegrityError" opens a ticket.
    """
    route = await _make_route(db)
    stop, = await _make_stops(db, 1)
    db.add(RouteStop(route_id=route.id, stop_id=stop.id, seq=1))
    await db.commit()

    response = await client.delete(f"/admin/stops/{stop.id}")
    assert response.status_code == status.HTTP_409_CONFLICT
    assert "1 route" in response.json()["detail"]


async def test_an_unused_stop_can_be_deleted(client, db) -> None:
    stop, = await _make_stops(db, 1)
    assert (await client.delete(f"/admin/stops/{stop.id}")).status_code == 204
    assert await db.get(Stop, stop.id) is None


async def test_a_stop_can_be_moved(client, db) -> None:
    stop, = await _make_stops(db, 1)
    patched = await client.patch(
        f"/admin/stops/{stop.id}", json={"lat": 23.8103, "lng": 90.4125}
    )
    assert patched.json()["lat"] == 23.8103
    # Untouched fields survive a partial edit.
    assert patched.json()["name"] == "Stop 0"


async def test_coordinates_off_the_planet_are_refused(client) -> None:
    response = await client.post(
        "/admin/stops", json={"name": "Nowhere", "lat": 91, "lng": 0}
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


# --- routes ----------------------------------------------------------------


async def test_a_name_may_repeat_only_in_the_other_direction(client) -> None:
    """Out-and-back pairs share a name; that is the point of the constraint."""
    body = {"name": "Uttara", "direction": "inbound"}
    assert (await client.post("/admin/routes", json=body)).status_code == 201

    assert (await client.post("/admin/routes", json=body)).status_code == 409

    outbound = await client.post(
        "/admin/routes", json={"name": "Uttara", "direction": "outbound"}
    )
    assert outbound.status_code == status.HTTP_201_CREATED


async def test_stops_are_numbered_by_position(client, db) -> None:
    route = await _make_route(db)
    stops = await _make_stops(db, 3)

    response = await client.put(
        f"/admin/routes/{route.id}/stops",
        json={
            "stops": [
                {"stop_id": str(stops[0].id), "scheduled_offset_min": 0},
                {"stop_id": str(stops[1].id), "scheduled_offset_min": 12},
                {"stop_id": str(stops[2].id), "scheduled_offset_min": 25},
            ]
        },
    )
    assert response.status_code == status.HTTP_200_OK
    assert [s["seq"] for s in response.json()["stops"]] == [1, 2, 3]
    assert [s["stop"]["name"] for s in response.json()["stops"]] == [
        "Stop 0",
        "Stop 1",
        "Stop 2",
    ]


async def test_reordering_a_route_does_not_collide_with_itself(client, db) -> None:
    """The reason this is a whole-list replace.

    `uq_route_stops_route_seq` makes any incremental reorder fail partway:
    moving stop 3 into position 2 needs position 2 free, which it is not until
    stop 2 has already moved. Deleting the old rows and flushing before the
    inserts is what keeps a straight reversal from raising.
    """
    route = await _make_route(db)
    stops = await _make_stops(db, 3)
    forward = {"stops": [{"stop_id": str(s.id)} for s in stops]}
    await client.put(f"/admin/routes/{route.id}/stops", json=forward)

    reversed_order = {"stops": [{"stop_id": str(s.id)} for s in reversed(stops)]}
    response = await client.put(f"/admin/routes/{route.id}/stops", json=reversed_order)

    assert response.status_code == status.HTTP_200_OK
    assert [s["seq"] for s in response.json()["stops"]] == [1, 2, 3]
    assert [s["stop"]["name"] for s in response.json()["stops"]] == [
        "Stop 2",
        "Stop 1",
        "Stop 0",
    ]


async def test_an_unknown_stop_leaves_the_existing_route_untouched(client, db) -> None:
    """A rejected replace must not have already deleted the old list.

    Validation happens before the delete, so a typo in one stop id costs an
    error message rather than a route that now has no stops at all.
    """
    route = await _make_route(db)
    stops = await _make_stops(db, 2)
    await client.put(
        f"/admin/routes/{route.id}/stops",
        json={"stops": [{"stop_id": str(s.id)} for s in stops]},
    )

    response = await client.put(
        f"/admin/routes/{route.id}/stops",
        json={"stops": [{"stop_id": str(stops[0].id)}, {"stop_id": str(uuid.uuid4())}]},
    )
    assert response.status_code == status.HTTP_404_NOT_FOUND

    surviving = await client.get(f"/fleet/routes/{route.id}")
    assert len(surviving.json()["stops"]) == 2


async def test_the_same_stop_twice_in_one_route_is_refused(client, db) -> None:
    route = await _make_route(db)
    stop, = await _make_stops(db, 1)

    response = await client.put(
        f"/admin/routes/{route.id}/stops",
        json={"stops": [{"stop_id": str(stop.id)}, {"stop_id": str(stop.id)}]},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "more than once" in response.json()["detail"]


async def test_retiring_a_route_hides_it_without_deleting_it(client, db) -> None:
    """No DELETE exists: `trips` reference routes with RESTRICT and every
    completed trip is history someone may report on."""
    route = await _make_route(db)

    patched = await client.patch(f"/admin/routes/{route.id}", json={"is_active": False})
    assert patched.json()["is_active"] is False

    assert (await client.get("/fleet/routes")).json() == []
    assert (await client.get("/fleet/routes?only_active=false")).json() != []
