"""Issuing the ticket a payment was for — against a real schema, not a stub.

Every other payment test in this suite checks a pure predicate: does this
response mean success, is this the right amount. All of them passed while
`issue_ticket` was constructing a `Ticket` with `qr_secret`, a column two
migrations dead. The result was a `TypeError` raised *after* the order was
marked paid, so the money moved at the gateway, the transaction died, and the
student got nothing — on every purchase, with the reconciler failing the same
way forever.

Predicate tests could not see that, and neither could a stub ticket. So these
build the actual tables and run the actual settlement. The assertion that the
issued keypair signs and verifies is deliberate: a `Ticket(...)` that merely
constructs would also pass with the two keys swapped, which fails silently at a
bus door rather than loudly here.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.commerce import (
    Order,
    OrderStatus,
    ProductType,
    Ticket,
    TicketProduct,
    TicketStatus,
)
from app.models.user import Student, User, UserRole, UserStatus
from app.services.boarding import QrPayload, build_qr, current_slice, new_nonce, verify_qr
from app.services.settlement import AMOUNT_MISMATCH, FAILED, PAID, apply_validation


@pytest.fixture
async def db():
    """A real schema. `create_all` is itself half the test: a model that
    references a column the migrations dropped cannot be built at all."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as session:
        yield session

    await engine.dispose()


async def _an_order(db: AsyncSession, *, price_paisa: int = 3000) -> Order:
    """A 30.00 BDT order from a real student for a real product."""
    user = User(
        email="student@ulab.edu.bd",
        password_hash="x",
        role=UserRole.student,
        name="Test Student",
        status=UserStatus.active,
    )
    user.student = Student(student_id_no="221011001")
    product = TicketProduct(
        type=ProductType.single,
        name="Single ride",
        price_paisa=price_paisa,
        ride_count=10,
        validity_days=30,
    )
    db.add_all([user, product])
    await db.flush()

    order = Order(
        student_id=user.student.id,
        product_id=product.id,
        amount_paisa=price_paisa,
        currency="BDT",
        status=OrderStatus.pending,
        idempotency_key=uuid.uuid4().hex,
        tran_id=f"UT-{uuid.uuid4().hex[:20]}",
    )
    db.add(order)
    await db.flush()
    return order


def _validation(**overrides) -> dict:
    """What SSLCommerz returns for a genuine 30.00 BDT payment."""
    return {
        "status": "VALID",
        "val_id": "2408011234567890",
        "currency_amount": "30.00",
        "currency_type": "BDT",
        "risk_level": "0",
        "bank_tran_id": "BANK123",
        "card_type": "BKASH-BKash",
        **overrides,
    }


async def _ticket_for(db: AsyncSession, order: Order) -> Ticket | None:
    return (
        await db.execute(select(Ticket).where(Ticket.order_id == order.id))
    ).scalar_one_or_none()


async def test_a_paid_order_produces_a_ticket(db: AsyncSession) -> None:
    """The whole point of taking the money."""
    order = await _an_order(db)

    outcome = await apply_validation(db, order, _validation())
    await db.commit()

    assert outcome == PAID
    assert order.status is OrderStatus.paid
    assert order.paid_at is not None

    ticket = await _ticket_for(db, order)
    assert ticket is not None
    assert ticket.status is TicketStatus.active
    assert ticket.student_id == order.student_id


async def test_the_ticket_carries_a_working_boarding_keypair(db: AsyncSession) -> None:
    """A ticket that cannot sign a boarding code is not a ticket.

    Signs with the stored private key and verifies with the stored public one,
    which is exactly what the student's device and the helper's do. Merely
    asserting both columns are non-empty would pass with the pair swapped — a
    failure that surfaces at a bus door instead of here.
    """
    order = await _an_order(db)
    await apply_validation(db, order, _validation())
    await db.commit()

    ticket = await _ticket_for(db, order)
    assert ticket is not None
    assert ticket.qr_private_key and ticket.qr_public_key
    assert ticket.qr_private_key != ticket.qr_public_key

    now = datetime.now(UTC).timestamp()
    code = build_qr(
        ticket.qr_private_key,
        QrPayload(
            ticket_id=str(ticket.id),
            passenger_count=1,
            time_slice=current_slice(now),
            nonce=new_nonce(),
        ),
    )
    assert verify_qr(code, ticket.qr_public_key, now).ticket_id == str(ticket.id)


async def test_every_ticket_gets_its_own_key(db: AsyncSession) -> None:
    """One shared key would let any ticket holder forge codes for every other."""
    first = await _an_order(db)
    await apply_validation(db, first, _validation())
    await db.commit()

    second = Order(
        student_id=first.student_id,
        product_id=first.product_id,
        amount_paisa=first.amount_paisa,
        currency="BDT",
        status=OrderStatus.pending,
        idempotency_key=uuid.uuid4().hex,
        tran_id=f"UT-{uuid.uuid4().hex[:20]}",
    )
    db.add(second)
    await db.flush()
    await apply_validation(db, second, _validation())
    await db.commit()

    a, b = await _ticket_for(db, first), await _ticket_for(db, second)
    assert a is not None and b is not None
    assert a.qr_private_key != b.qr_private_key
    assert a.qr_public_key != b.qr_public_key


async def test_rides_are_copied_from_the_product(db: AsyncSession) -> None:
    """`rides_remaining` starts full; the redemption path counts it down."""
    order = await _an_order(db)
    await apply_validation(db, order, _validation())
    await db.commit()

    ticket = await _ticket_for(db, order)
    assert ticket is not None
    assert ticket.rides_total == 10
    assert ticket.rides_remaining == 10
    assert ticket.valid_from < ticket.valid_to


async def test_a_failed_payment_issues_nothing(db: AsyncSession) -> None:
    order = await _an_order(db)

    outcome = await apply_validation(db, order, _validation(status="FAILED"))
    await db.commit()

    assert outcome == FAILED
    assert order.status is OrderStatus.failed
    assert await _ticket_for(db, order) is None


async def test_underpaying_issues_nothing(db: AsyncSession) -> None:
    """A genuinely VALID response can be for an amount the payer chose."""
    order = await _an_order(db)

    outcome = await apply_validation(db, order, _validation(currency_amount="1.00"))
    await db.commit()

    assert outcome == AMOUNT_MISMATCH
    assert order.status is OrderStatus.failed
    assert await _ticket_for(db, order) is None
