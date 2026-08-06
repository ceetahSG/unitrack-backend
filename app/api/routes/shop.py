"""Buying a ticket: catalogue, orders, payment settlement, wallet.

The payment flow, and why each step is where it is:

    POST /shop/orders          create the order, open an SSLCommerz session
    (student pays on the gateway's own page)
    POST /shop/payments/return the gateway sends the browser back here
    GET  /shop/tickets         the ticket is now in the wallet

**The return endpoint is unauthenticated, and that is not a hole.** SSLCommerz
redirects the student's browser with a form POST, which carries no Authorization
header and no cookie we control. Security does not come from authenticating that
request — anyone can forge it by typing a URL. It comes from what happens next:
the `val_id` in the request is checked server-to-server against SSLCommerz, and
the settled amount and currency are compared with the order's own. A forged
return fails validation and issues nothing.

That is also why the order, not the request, decides who gets the ticket: the
student is read from the order row we created earlier, never from the callback.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_authenticated, require_student
from app.core.authz import Principal
from app.core.config import settings
from app.core.sslcommerz import (
    GatewayError,
    SslCommerzClient,
    amount_matches,
    payment_succeeded,
    risk_flagged,
)
from app.db.session import get_db
from app.models.commerce import Order, OrderStatus, Ticket, TicketProduct, TicketStatus
from app.models.user import Student, User
from app.schemas.commerce import CheckoutOut, OrderCreate, OrderOut, ProductOut, TicketOut

logger = logging.getLogger("unitrack.shop")

router = APIRouter(prefix="/shop", tags=["shop"])

_gateway = SslCommerzClient()


async def _student_for(db: AsyncSession, principal: Principal) -> Student:
    student = (
        await db.execute(select(Student).where(Student.user_id == principal.user_id))
    ).scalar_one_or_none()
    if student is None:
        # A student-role account with no student row is a data fault, not a
        # permission problem; saying "forbidden" would send someone hunting in
        # the wrong place.
        raise HTTPException(status.HTTP_409_CONFLICT, "Account has no student profile")
    return student


def _return_urls() -> dict[str, str]:
    base = settings.public_base_url.rstrip("/")
    # One endpoint for all three outcomes: the gateway tells us which happened,
    # and three near-identical handlers would drift apart.
    return {
        "success_url": f"{base}/shop/payments/return",
        "fail_url": f"{base}/shop/payments/return",
        "cancel_url": f"{base}/shop/payments/return",
    }


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@router.get(
    "/products",
    response_model=list[ProductOut],
    dependencies=[Depends(require_authenticated)],
)
async def list_products(db: AsyncSession = Depends(get_db)) -> list[TicketProduct]:
    """What is for sale. Readable by any signed-in account so the helper app can
    show a student what they should have bought."""
    stmt = select(TicketProduct).where(TicketProduct.active.is_(True)).order_by(
        TicketProduct.price_paisa
    )
    return list((await db.execute(stmt)).scalars())


# ---------------------------------------------------------------------------
# Purchase
# ---------------------------------------------------------------------------


@router.post("/orders", response_model=CheckoutOut, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreate,
    principal: Principal = Depends(require_student),
    db: AsyncSession = Depends(get_db),
) -> CheckoutOut:
    """Start a purchase and return the gateway URL to send the student to.

    Idempotent on `idempotency_key`: a retried or double-tapped request returns
    the original order rather than charging twice. The uniqueness is enforced by
    the database, because a check-then-insert loses the race that makes this
    necessary in the first place.
    """
    student = await _student_for(db, principal)

    existing = (
        await db.execute(select(Order).where(Order.idempotency_key == payload.idempotency_key))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.student_id != student.id:
            # Someone else's key. Refuse rather than reveal that it exists.
            raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency key already used")
        if existing.status is OrderStatus.paid:
            raise HTTPException(status.HTTP_409_CONFLICT, "Order already paid")
        return await _open_checkout(db, existing, student)

    product = await db.get(TicketProduct, payload.product_id)
    if product is None or not product.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not available")

    order = Order(
        student_id=student.id,
        product_id=product.id,
        # Copied, not referenced: a price change tomorrow must not rewrite what
        # this student agreed to pay today.
        amount_paisa=product.price_paisa,
        currency="BDT",
        status=OrderStatus.initiated,
        idempotency_key=payload.idempotency_key,
        tran_id=f"UT-{uuid.uuid4().hex[:20]}",
    )
    db.add(order)
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race against a concurrent identical request; that request's
        # order is the real one.
        await db.rollback()
        winner = (
            await db.execute(select(Order).where(Order.idempotency_key == payload.idempotency_key))
        ).scalar_one()
        return await _open_checkout(db, winner, student)

    await db.refresh(order)
    return await _open_checkout(db, order, student)


async def _open_checkout(db: AsyncSession, order: Order, student: Student) -> CheckoutOut:
    product = await db.get(TicketProduct, order.product_id)
    user = await db.get(User, student.user_id)
    if product is None or user is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Order references missing records")

    try:
        checkout_url = await _gateway.create_session(
            tran_id=order.tran_id,
            amount_paisa=order.amount_paisa,
            currency=order.currency,
            **_return_urls(),
            ipn_url=None,
            customer_name=user.name,
            customer_email=user.email,
            customer_phone=user.phone,
            product_name=product.name,
        )
    except GatewayError as exc:
        logger.error("checkout failed for order %s: %s", order.id, exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Payment gateway unavailable, try again"
        ) from exc

    order.status = OrderStatus.pending
    await db.commit()

    return CheckoutOut(
        order_id=order.id,
        tran_id=order.tran_id,
        amount_paisa=order.amount_paisa,
        currency=order.currency,
        status=order.status,
        checkout_url=checkout_url,
    )


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


@router.api_route("/payments/return", methods=["GET", "POST"], include_in_schema=True)
async def payment_return(request: Request, db: AsyncSession = Depends(get_db)):
    """Where the gateway sends the student's browser, whatever the outcome.

    Unauthenticated by necessity and safe by construction — see the module
    docstring. Nothing here trusts a field in the request except as a lookup
    key; the decision to issue a ticket rests entirely on the server-to-server
    validation call.
    """
    form = dict(await request.form()) if request.method == "POST" else dict(request.query_params)
    tran_id = str(form.get("tran_id") or "")
    if not tran_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing tran_id")

    order = (
        await db.execute(select(Order).where(Order.tran_id == tran_id))
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown transaction")

    # Refreshing the success page must not be an error, and must not issue a
    # second ticket.
    if order.status is OrderStatus.paid:
        return _finish(order, "paid")

    gateway_status = str(form.get("status") or "").upper()
    if gateway_status in {"FAILED", "CANCELLED"}:
        order.status = (
            OrderStatus.cancelled if gateway_status == "CANCELLED" else OrderStatus.failed
        )
        await db.commit()
        return _finish(order, order.status.value)

    val_id = str(form.get("val_id") or "")
    if not val_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing val_id")

    try:
        validation = await _gateway.validate(val_id)
    except GatewayError as exc:
        # Leave the order pending rather than failing it: the money may well
        # have moved, and the reconciler needs to see it as unsettled.
        logger.error("validation unreachable for order %s: %s", order.id, exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Could not confirm payment, please check your wallet later"
        ) from exc

    order.raw_payload = validation
    order.gateway_val_id = val_id
    order.gateway_bank_tran_id = validation.get("bank_tran_id")
    order.gateway_card_type = validation.get("card_type")

    if not payment_succeeded(validation):
        order.status = OrderStatus.failed
        await db.commit()
        return _finish(order, "failed")

    # A VALID status for the wrong amount is a successful attack if only the
    # status is checked, so the figures are compared before anything is issued.
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
        await db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payment amount mismatch")

    if risk_flagged(validation):
        # SSLCommerz wants this reviewed. Hold it rather than hand over a ticket
        # that may be charged back.
        logger.warning("order %s flagged for risk review", order.id)
        order.status = OrderStatus.pending
        await db.commit()
        return _finish(order, "under_review")

    order.status = OrderStatus.paid
    order.paid_at = datetime.now(UTC)
    await _issue_ticket(db, order)
    await db.commit()
    return _finish(order, "paid")


async def _issue_ticket(db: AsyncSession, order: Order) -> None:
    """Create the ticket for a paid order. At most one, ever.

    `tickets.order_id` is unique, so a replayed callback that slipped past the
    status check still cannot mint a second ticket — the database refuses it.
    """
    product = await db.get(TicketProduct, order.product_id)
    if product is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Order references a missing product")

    now = datetime.now(UTC)
    db.add(
        Ticket(
            order_id=order.id,
            student_id=order.student_id,
            product_id=product.id,
            rides_total=product.ride_count,
            rides_remaining=product.ride_count,
            valid_from=now,
            valid_to=now + timedelta(days=product.validity_days),
            # Per-ticket HMAC key for the rotating boarding QR (spec §7.2).
            # Generated now so the column is never null on a live ticket.
            qr_secret=secrets.token_hex(32),
            status=TicketStatus.active,
        )
    )


def _finish(order: Order, outcome: str):
    """Send the browser onward, or answer plainly if there is nowhere to go."""
    if settings.checkout_return_url:
        target = (
            f"{settings.checkout_return_url.rstrip('/')}"
            f"?order={order.id}&status={outcome}"
        )
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    return {"order_id": str(order.id), "status": outcome}


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    principal: Principal = Depends(require_student),
    db: AsyncSession = Depends(get_db),
) -> list[Order]:
    student = await _student_for(db, principal)
    stmt = (
        select(Order).where(Order.student_id == student.id).order_by(Order.created_at.desc())
    )
    return list((await db.execute(stmt)).scalars())


@router.get("/tickets", response_model=list[TicketOut])
async def list_tickets(
    principal: Principal = Depends(require_student),
    db: AsyncSession = Depends(get_db),
) -> list[Ticket]:
    """The student's wallet. Scoped to the caller — never takes an id from the
    request, so one student cannot read another's tickets."""
    student = await _student_for(db, principal)
    stmt = (
        select(Ticket).where(Ticket.student_id == student.id).order_by(Ticket.created_at.desc())
    )
    return list((await db.execute(stmt)).scalars())
