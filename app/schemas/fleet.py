import uuid

from pydantic import BaseModel, ConfigDict, Field

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


class StopCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class StopUpdate(BaseModel):
    """Rename or move a stop. Unset fields are left alone.

    Moving one changes where every route through it is drawn, which is correct:
    a stop that was mapped to the wrong side of a road should be fixed once,
    not worked around in each route.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


class RouteStopOut(BaseModel):
    """A stop in the context of one route — sequence and timing included."""

    seq: int
    scheduled_offset_min: int | None
    stop: StopOut


class RouteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    direction: RouteDirection
    polyline: str | None = None
    is_active: bool = True


class RouteUpdate(BaseModel):
    """A partial edit. Unset fields are left alone; `polyline` may be cleared."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    direction: RouteDirection | None = None
    polyline: str | None = None
    is_active: bool | None = None


class RouteStopIn(BaseModel):
    """One stop's place in a route.

    No `seq` field — position in the submitted list *is* the sequence. Letting a
    client send both an order and a number invites the two to disagree, and
    `uq_route_stops_route_seq` turns that disagreement into a 500. The server
    numbers them 1..n instead, which also makes reordering a matter of sending
    the list in the new order.
    """

    stop_id: uuid.UUID
    # Minutes from trip start. This is what makes a per-stop ETA possible before
    # any traffic data exists, so it is worth filling in even roughly.
    scheduled_offset_min: int | None = Field(default=None, ge=0)


class RouteStopsReplace(BaseModel):
    """The complete ordered stop list for a route.

    Replace-the-whole-list rather than add/remove/move endpoints. The sequence
    numbers are unique per route, so any incremental edit collides with itself
    partway through — moving stop 3 to position 2 needs position 2 free, which
    it is not until stop 2 has already moved. Sending the final order sidesteps
    the entire problem.
    """

    stops: list[RouteStopIn] = Field(min_length=1, max_length=200)


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
