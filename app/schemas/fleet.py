import uuid

from pydantic import BaseModel, ConfigDict

from app.models.fleet import BusStatus, RouteDirection


class BusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reg_no: str
    nickname: str | None
    capacity: int
    status: BusStatus


class BusCreate(BaseModel):
    reg_no: str
    nickname: str | None = None
    capacity: int = 40
    status: BusStatus = BusStatus.active


class BusListCreate(BaseModel):
    buses: list[BusCreate]


class StopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    lat: float
    lng: float


class RouteStopOut(BaseModel):
    """A stop in the context of one route — sequence and timing included."""

    seq: int
    scheduled_offset_min: int | None
    stop: StopOut


class RouteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    direction: RouteDirection
    is_active: bool


class RouteDetailOut(RouteOut):
    """A route plus its ordered stops and drawn shape.

    `polyline` is a Google-encoded polyline — a few thousand road points
    compressed to a string, so the map can draw the actual road rather than
    straight lines between stops without shipping a large coordinate array.
    """

    polyline: str | None
    stops: list[RouteStopOut]
