import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.commerce import OrderStatus, ProductType, TicketStatus


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: ProductType
    name: str
    price_paisa: int
    ride_count: int | None
    validity_days: int
    route_scope: uuid.UUID | None

    @property
    def price_bdt(self) -> str:
        return f"{self.price_paisa / 100:.2f}"


class OrderCreate(BaseModel):
    product_id: uuid.UUID
    # Supplied by the client so a retried request is recognised as the same
    # purchase. Without it, a double-tapped Buy button is two orders and two
    # charges — the retry cannot be detected server-side after the fact.
    idempotency_key: str = Field(min_length=8, max_length=64)


class CheckoutOut(BaseModel):
    """Where to send the student to pay."""

    order_id: uuid.UUID
    tran_id: str
    amount_paisa: int
    currency: str
    status: OrderStatus
    checkout_url: str


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    amount_paisa: int
    currency: str
    status: OrderStatus
    tran_id: str
    paid_at: datetime.datetime | None
    created_at: datetime.datetime


class TicketOut(BaseModel):
    # `qr_secret` is deliberately absent. It is the HMAC key for the boarding
    # QR, and the boarding flow will hand it over through its own endpoint with
    # its own rules — a wallet listing must not leak every ticket's key.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    product_id: uuid.UUID
    rides_total: int | None
    rides_remaining: int | None
    valid_from: datetime.datetime
    valid_to: datetime.datetime
    status: TicketStatus
