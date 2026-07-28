"""Read-side GPS queries backed by Elasticsearch (spec §5.1 revisit).

Demonstrates the ES payoff over Redis/Postgres: geo_distance search. Redis
still owns single-bus "latest position"; ES owns "which buses are near me".
"""

from elasticsearch import AsyncElasticsearch
from fastapi import APIRouter, Depends, Query

from app.api.deps import require_authenticated
from app.core.elasticsearch import GPS_INDEX, get_es
from datetime import datetime
from typing import Optional
from sqlalchemy import select
from app.models.fleet import Bus, Trip
from app.db.session import get_db
from sqlalchemy.ext.asyncio import AsyncSession
import uuid
# Update existing import:
from app.schemas.gps import GpsAccepted, GpsPoint, BusHistoryPathOut  # Add GpsPoint, BusHistoryPathOut

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

@router.get("/bus/{bus_id}/history", response_model=BusHistoryPathOut)
async def get_bus_history_path(
    bus_id: uuid.UUID,
    from_ts: datetime = Query(
        ...,
        alias="from_timestamp",
        description="Start timestamp (ISO 8601)"
    ),
    to_ts: datetime = Query(
        ...,
        alias="to_timestamp",
        description="End timestamp (ISO 8601)"
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
    
    # Validate bus exists
    bus = await db.get(Bus, bus_id)
    if bus is None:
        raise HTTPException(status_code=404, detail=f"Bus {bus_id} not found")
    
    # Validate time range
    if from_ts >= to_ts:
        raise HTTPException(status_code=400, detail="from_timestamp must be before to_timestamp")
    
    # Validate trip if provided
    if trip_id:
        stmt = select(Trip).where(Trip.id == trip_id, Trip.bus_id == bus_id)
        trip = await db.scalar(stmt)
        if trip is None:
            raise HTTPException(status_code=404, detail=f"Trip {trip_id} not found for bus {bus_id}")
    
    # Build Elasticsearch query
    filters = [
        {"term": {"bus_id": str(bus_id)}},
        {"range": {"ts": {"gte": from_ts.isoformat(), "lte": to_ts.isoformat()}}}
    ]
    if trip_id:
        filters.append({"term": {"trip_id": str(trip_id)}})
    
    # Query Elasticsearch
    res = await es.search(
        index="gps_points",
        size=limit,
        query={"bool": {"must": filters}},
        sort=[{"ts": {"order": "asc"}}],
        _source=["bus_id", "trip_id", "ts", "lat", "lng", "speed", "heading", "accuracy"]
    )
    
    # Transform results
    points = []
    trip_id_from_data = None
    
    for hit in res["hits"]["hits"]:
        source = hit["_source"]
        
        if not trip_id_from_data and source.get("trip_id"):
            trip_id_from_data = source["trip_id"]
        
        # Parse timestamp
        ts_str = source.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        
        # Create point
        point = GpsPoint(
            timestamp=ts,
            latitude=float(source.get("lat", 0)),
            longitude=float(source.get("lng", 0)),
            speed=float(source["speed"]) if source.get("speed") else None,
            heading=float(source["heading"]) if source.get("heading") else None,
            accuracy=float(source["accuracy"]) if source.get("accuracy") else None,
        )
        points.append(point)
    
    # Convert trip_id UUID
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
        path=points
    )