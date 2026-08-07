import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.commerce import OrderStatus, ProductType, RedemptionFlag, TicketStatus


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
    # `qr_private_key` is deliberately absent. It signs boarding codes, so a
    # wallet listing that carried it would hand over every one of the caller's
    # signing keys on a screen that only needs to show dates and ride counts.
    # It leaves the server through `QrMaterialOut` alone, one ticket at a time.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    product_id: uuid.UUID
    rides_total: int | None
    rides_remaining: int | None
    valid_from: datetime.datetime
    valid_to: datetime.datetime
    status: TicketStatus


class QrMaterialOut(BaseModel):
    """Everything a student's device needs to render boarding codes offline.

    The private key leaves the server exactly once per sync, over an
    authenticated request, to the account that owns the ticket. That is the
    accepted trade in spec §7.5: a code that works with no signal has to be
    generated on the device, and generating it requires the key.

    `server_time` is the clock-offset anchor. A phone whose clock is wrong
    would otherwise sign codes in the wrong time slice and be rejected at the
    door with nothing to explain why.
    """

    ticket_id: uuid.UUID
    qr_private_key: str
    slice_seconds: int
    server_time: datetime.datetime
    passenger_count: int
    valid_to: datetime.datetime


class ManifestTicketOut(BaseModel):
    """One row of the helper's offline ticket manifest (spec §7.5).

    Carries the **public** key only. A lost or stolen helper phone therefore
    leaks nothing that can forge a boarding code — it can verify codes, not
    create them.

    `rides_remaining` is a snapshot for display and for the dead-phone manual
    fallback. The server value is authoritative; this one is as fresh as the
    helper's last sync.
    """

    model_config = ConfigDict(from_attributes=True)

    ticket_id: uuid.UUID
    qr_public_key: str
    student_name: str
    student_id_no: str
    rides_remaining: int | None
    valid_to: datetime.datetime
    status: TicketStatus


class RedemptionIn(BaseModel):
    """One boarding a helper's device recorded, online or hours earlier."""

    code: str = Field(min_length=8, max_length=512)
    device_id: str = Field(min_length=1, max_length=128)
    # The device's own clock at the scan. Not trusted for validity — the time
    # slice inside the signed code decides that — but recorded so an offline
    # trip does not appear to have happened at sync time.
    redeemed_at: datetime.datetime
    trip_id: uuid.UUID | None = None


class RedemptionBatchIn(BaseModel):
    # Batched because a helper coming back into signal may have a route's worth
    # queued. Capped so one sync cannot monopolise a worker.
    redemptions: list[RedemptionIn] = Field(min_length=1, max_length=100)


class RedemptionResultOut(BaseModel):
    """What happened to one submitted boarding.

    `accepted` tells the device whether to drop the row from its queue.
    A rejected code is dropped too — retrying a forged or expired code forever
    would be a queue that never drains.
    """

    nonce: str | None
    accepted: bool
    reason: str
    ticket_id: uuid.UUID | None = None
    rides_remaining: int | None = None
    flag: RedemptionFlag | None = None


class RedemptionBatchOut(BaseModel):
    results: list[RedemptionResultOut]
