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
