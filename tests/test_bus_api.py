import uuid

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_db, get_principal
from app.core.authz import Principal
from app.db.base import Base
from app.main import create_app
from app.models.fleet import Bus, BusStatus
from app.models.user import UserRole, UserStatus
from app.schemas.fleet import BusCreate, BusListCreate


def test_bus_create_schemas() -> None:
    bus_in = BusCreate(reg_no="DHK-101")
    assert bus_in.reg_no == "DHK-101"
    assert bus_in.capacity == 40
    assert bus_in.status == BusStatus.active
    assert bus_in.nickname is None

    bus_list_in = BusListCreate(
        buses=[
            BusCreate(reg_no="DHK-101", capacity=50),
            BusCreate(reg_no="DHK-102", nickname="Express"),
        ]
    )
    assert len(bus_list_in.buses) == 2
    assert bus_list_in.buses[0].capacity == 50
    assert bus_list_in.buses[1].nickname == "Express"


@pytest.fixture
async def in_memory_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def admin_principal():
    return Principal(
        user_id=uuid.uuid4(),
        role=UserRole.admin,
        status=UserStatus.active,
    )


@pytest.fixture
def app_with_db(in_memory_db, admin_principal):
    app = create_app()

    async def _get_db_override():
        yield in_memory_db

    async def _get_principal_override():
        return admin_principal

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_principal] = _get_principal_override
    return app


@pytest.mark.asyncio
async def test_create_single_bus(app_with_db, in_memory_db: AsyncSession):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_db), base_url="http://test"
    ) as client:
        payload = {"reg_no": "BUS-001", "nickname": "Red Line", "capacity": 45}
        response = await client.post("/admin/buses", json=payload)
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["reg_no"] == "BUS-001"
        assert data["nickname"] == "Red Line"
        assert data["capacity"] == 45
        assert data["status"] == "active"
        assert "id" in data

        # Duplicate reg_no should return 409
        dup_response = await client.post("/admin/buses", json=payload)
        assert dup_response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.asyncio
async def test_create_bus_list(app_with_db, in_memory_db: AsyncSession):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_db), base_url="http://test"
    ) as client:
        payload = {
            "buses": [
                {"reg_no": "BUS-101", "nickname": "Route A1", "capacity": 30},
                {"reg_no": "BUS-102", "nickname": "Route A2", "capacity": 40},
            ]
        }
        # Test POST /admin/buses/batch
        response = await client.post("/admin/buses/batch", json=payload)
        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert len(data) == 2
        assert data[0]["reg_no"] == "BUS-101"
        assert data[1]["reg_no"] == "BUS-102"

        # Verify items in database
        stmt = select(Bus)
        result = await in_memory_db.execute(stmt)
        buses = result.scalars().all()
        assert len(buses) == 2

        # Test POST /admin/buses/list alias endpoint
        payload_alias = {
            "buses": [
                {"reg_no": "BUS-201", "nickname": "Route B1"},
                {"reg_no": "BUS-202", "nickname": "Route B2"},
            ]
        }
        alias_response = await client.post("/admin/buses/list", json=payload_alias)
        assert alias_response.status_code == status.HTTP_201_CREATED
        assert len(alias_response.json()) == 2


@pytest.mark.asyncio
async def test_create_bus_list_duplicates(app_with_db):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_db), base_url="http://test"
    ) as client:
        # Duplicate within request payload -> 400
        payload_internal_dup = {
            "buses": [
                {"reg_no": "BUS-DUP", "nickname": "Bus 1"},
                {"reg_no": "BUS-DUP", "nickname": "Bus 2"},
            ]
        }
        res_400 = await client.post("/admin/buses/batch", json=payload_internal_dup)
        assert res_400.status_code == status.HTTP_400_BAD_REQUEST

        # Already existing in DB -> 409
        await client.post("/admin/buses", json={"reg_no": "BUS-EXISTING"})
        payload_existing_dup = {
            "buses": [
                {"reg_no": "BUS-EXISTING", "nickname": "Existing Bus"},
                {"reg_no": "BUS-NEW", "nickname": "New Bus"},
            ]
        }
        res_409 = await client.post("/admin/buses/batch", json=payload_existing_dup)
        assert res_409.status_code == status.HTTP_409_CONFLICT
