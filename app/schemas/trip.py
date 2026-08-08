import datetime
import uuid

from pydantic import BaseModel, ConfigDict

from app.models.fleet import TripStatus


class TripStartRequest(BaseModel):
    """What the helper picks in the app before pressing Start."""

    bus_id: uuid.UUID
    route_id: uuid.UUID


class TripOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    route_id: uuid.UUID
    bus_id: uuid.UUID
    helper_id: uuid.UUID
    service_date: datetime.date
    status: TripStatus
    actual_start: datetime.datetime | None
    actual_end: datetime.datetime | None


class ActiveTripOut(BaseModel):
    """The helper app's answer to "am I already tracking?" after a restart."""

    trip_id: uuid.UUID
    bus_id: uuid.UUID
    route_id: uuid.UUID


class ArrivalOut(BaseModel):
    """One bus reaching one stop (spec §7.4)."""

    stop_id: uuid.UUID
    seq: int
    eta: datetime.datetime
    # Pre-rounded so every client shows the same number. Deriving minutes in the
    # browser means one phone reads "4 min" and the next "5 min" from the same
    # payload, purely because their clocks differ.
    eta_minutes: int
    # "live" — from how fast the bus is actually moving, good for the next stop
    # or two. "scheduled" — the timetable shifted by the delay measured so far.
    # Surfaced because they deserve different amounts of trust from someone
    # deciding whether to run for it.
    basis: str
    distance_km: float


class TripEtaOut(BaseModel):
    """Every remaining arrival for one live trip — the admin fleet view."""

    trip_id: uuid.UUID
    route_id: uuid.UUID
    bus_id: uuid.UUID
    computed_at: datetime.datetime
    arrivals: list[ArrivalOut]


class BusArrivalOut(ArrivalOut):
    """An arrival at a stop, told from the stop's point of view.

    Carries the bus and route because a student waiting at Farmgate needs to
    know *which* bus is four minutes away — two routes serve most stops, and
    only one of them is going where they are going.
    """

    trip_id: uuid.UUID
    route_id: uuid.UUID
    route_name: str
    bus_id: uuid.UUID


class StopArrivalsOut(BaseModel):
    """What a student standing at a stop actually asked."""

    stop_id: uuid.UUID
    stop_name: str
    as_of: datetime.datetime
    arrivals: list[BusArrivalOut]
