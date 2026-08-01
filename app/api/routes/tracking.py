"""Read-side tracking: where the buses are, and when they arrive.

Two different questions with two different backing stores, which is the whole
point of the split:

- **"which buses are near me"** — Elasticsearch, via geo_distance. This is the
  payoff over Redis/Postgres that §5.1 was revisited for.
- **"when does one reach my stop"** — Redis, read straight from what the ETA
  engine precomputed. The answer depends on the bus, not the asker, so a
  hundred students watching one stop share one computation rather than each
  paying for a route query, a fix-history query and a distance walk.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Optional

from elasticsearch import AsyncElasticsearch
<<<<<<< HEAD
from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
=======
from fastapi import APIRouter, Depends, Query, HTTPException
>>>>>>> e118d42 (add routes)

from app.api.deps import require_authenticated
from app.core.elasticsearch import GPS_INDEX, get_es
from app.core.redis import get_redis, trip_eta_key
from app.db.session import get_db
from app.models.fleet import Bus, Route, RouteStop, Stop, Trip, TripStatus
from app.schemas.gps import BusHistoryPathOut, GpsPoint
from app.schemas.trip import BusArrivalOut, StopArrivalsOut, TripEtaOut

# Any signed-in, active account may look up buses — students, helpers, admins
# all need it. Not public: live vehicle positions are the fleet's whereabouts,
# and an unauthenticated endpoint hands them to anyone who finds the URL.
router = APIRouter(
    prefix="/track",
    tags=["tracking"],
    dependencies=[Depends(require_authenticated)],
)


@router.get("/nearby")
async def nearby_buses(
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    radius_km: float = Query(default=5, gt=0, le=50),
    limit: int = Query(default=20, ge=1, le=100),
    es: AsyncElasticsearch = Depends(get_es),
) -> dict:
    """Buses with a recent fix within `radius_km`, closest first.

    `collapse` on bus_id returns one hit per bus; the `_geo_distance` sort makes
    that hit the closest point and exposes its distance.
    """
    origin = {"lat": lat, "lon": lng}
    res = await es.search(
        index=GPS_INDEX,
        size=limit,
        query={"geo_distance": {"distance": f"{radius_km}km", "location": origin}},
        collapse={"field": "bus_id"},
        sort=[{"_geo_distance": {"location": origin, "order": "asc", "unit": "km"}}],
    )
    buses = [
        {
            "bus_id": h["_source"]["bus_id"],
            "location": h["_source"]["location"],
            "ts": h["_source"]["ts"],
            "speed": h["_source"].get("speed"),
            "distance_km": round(h["sort"][0], 3),
        }
        for h in res["hits"]["hits"]
    ]
    return {"origin": origin, "radius_km": radius_km, "count": len(buses), "buses": buses}


# ---------------------------------------------------------------------------
# Arrival times (spec §7.4)
# ---------------------------------------------------------------------------


async def _cached_eta(r: Redis, trip_id: str) -> dict | None:
    """One trip's precomputed arrivals, or None if there are none to read.

    A miss is ordinary, not an error: the trip may have just started, the bus
    may have no fixes yet, or the engine may not have run since. Every caller
    treats absent as "nothing to say about this bus" rather than failing, so a
    Redis outage costs arrival times and nothing else.
    """
    try:
        raw = await r.get(trip_eta_key(trip_id))
    except Exception:  # noqa: BLE001 — ETAs are an enhancement, never a gate
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


@router.get("/trips/{trip_id}/eta", response_model=TripEtaOut)
async def trip_eta(
    trip_id: uuid.UUID,
    r: Redis = Depends(get_redis),
) -> TripEtaOut:
    """Every remaining arrival for one live trip — what the fleet map draws."""
    cached = await _cached_eta(r, str(trip_id))
    if cached is None:
        # Deliberately not an empty 200: "this bus has no estimate right now"
        # and "this bus is arriving nowhere" would otherwise look identical,
        # and a map would render the second as a bus that has finished.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "No arrival estimate for this trip yet"
        )
    return TripEtaOut.model_validate(cached)


@router.get("/stops/{stop_id}/arrivals", response_model=StopArrivalsOut)
async def stop_arrivals(
    stop_id: uuid.UUID,
    limit: int = Query(default=5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    r: Redis = Depends(get_redis),
) -> StopArrivalsOut:
    """The next buses reaching one stop, soonest first.

    The question a student standing at a stop actually has, and the reason the
    ETA engine exists. The live map answers "where is it", which is not the same
    thing — a bus two kilometres away on an open road and one two kilometres
    away in Farmgate traffic are twenty minutes apart.

    Reads only Redis for the estimates themselves. Postgres is touched once, for
    the names, because "4 min" is useless without knowing which route it is on.
    """
    stop = await db.get(Stop, stop_id)
    if stop is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown stop")

    # Live trips whose route includes this stop. A trip on a route that does not
    # serve it can never arrive, so there is no point reading its estimate.
    stmt = (
        select(Trip.id, Trip.route_id, Trip.bus_id, Route.name)
        .join(Route, Route.id == Trip.route_id)
        .join(RouteStop, RouteStop.route_id == Route.id)
        .where(Trip.status == TripStatus.live, RouteStop.stop_id == stop_id)
    )
    candidates = (await db.execute(stmt)).all()

    arrivals: list[BusArrivalOut] = []
    for trip_id, route_id, bus_id, route_name in candidates:
        cached = await _cached_eta(r, str(trip_id))
        if cached is None:
            continue
        for item in cached.get("arrivals", []):
            if item.get("stop_id") != str(stop_id):
                continue
            arrivals.append(
                BusArrivalOut(
                    stop_id=stop_id,
                    seq=item["seq"],
                    eta=item["eta"],
                    eta_minutes=item["eta_minutes"],
                    basis=item["basis"],
                    distance_km=item["distance_km"],
                    trip_id=trip_id,
                    route_id=route_id,
                    route_name=route_name,
                    bus_id=bus_id,
                )
            )
            # A stop appears at most once per route, so the first match is the
            # only one this trip can offer.
            break

    arrivals.sort(key=lambda a: a.eta)
    return StopArrivalsOut(
        stop_id=stop.id,
        stop_name=stop.name,
        as_of=datetime.now(UTC),
        arrivals=arrivals[:limit],
    )


@router.get("/bus/{bus_id}/history", response_model=BusHistoryPathOut)
async def get_bus_history_path(
    bus_id: uuid.UUID,
    from_ts: datetime = Query(
        ..., alias="from_timestamp", description="Start timestamp (ISO 8601)"
    ),
    to_ts: datetime = Query(
        ..., alias="to_timestamp", description="End timestamp (ISO 8601)"
    ),
    trip_id: Optional[uuid.UUID] = Query(None, description="Optional trip filter"),
    limit: int = Query(default=500, ge=1, le=5000, description="Max points to return"),
    es: AsyncElasticsearch = Depends(get_es),
    db: AsyncSession = Depends(get_db),
) -> BusHistoryPathOut:
    """Get complete GPS path (history) for a bus within time range.

    Example:
    GET /track/bus/550e8400.../history?from_timestamp=2026-07-21T08:00:00Z&to_timestamp=2026-07-21T18:00:00Z
    """

    bus = await db.get(Bus, bus_id)
    if bus is None:
        raise HTTPException(status_code=404, detail=f"Bus {bus_id} not found")

    if from_ts >= to_ts:
        raise HTTPException(
            status_code=400, detail="from_timestamp must be before to_timestamp"
        )

    if trip_id:
        stmt = select(Trip).where(Trip.id == trip_id, Trip.bus_id == bus_id)
        trip = await db.scalar(stmt)
        if trip is None:
            raise HTTPException(
                status_code=404, detail=f"Trip {trip_id} not found for bus {bus_id}"
            )

    filters = [
        {"term": {"bus_id": str(bus_id)}},
        {"range": {"ts": {"gte": from_ts.isoformat(), "lte": to_ts.isoformat()}}},
    ]
    if trip_id:
        filters.append({"term": {"trip_id": str(trip_id)}})

    res = await es.search(
        index="gps_points",
        size=limit,
        query={"bool": {"must": filters}},
        sort=[{"ts": {"order": "asc"}}],
        _source=["bus_id", "trip_id", "ts", "lat", "lng", "speed", "heading", "accuracy"],
    )

    points: list[GpsPoint] = []
    trip_id_from_data = None

    for hit in res["hits"]["hits"]:
        source = hit["_source"]

        if not trip_id_from_data and source.get("trip_id"):
            trip_id_from_data = source["trip_id"]

        ts_str = source.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        points.append(
            GpsPoint(
                timestamp=ts,
                latitude=float(source.get("lat", 0)),
                longitude=float(source.get("lng", 0)),
                speed=float(source["speed"]) if source.get("speed") else None,
                heading=float(source["heading"]) if source.get("heading") else None,
                accuracy=float(source["accuracy"]) if source.get("accuracy") else None,
            )
        )

    trip_id_result = None
    if trip_id_from_data:
        try:
            trip_id_result = uuid.UUID(trip_id_from_data)
        except (ValueError, TypeError):
            pass

    return BusHistoryPathOut(
        bus_id=bus_id,
        trip_id=trip_id_result,
        from_timestamp=from_ts,
        to_timestamp=to_ts,
        point_count=len(points),
        path=points,
    )
