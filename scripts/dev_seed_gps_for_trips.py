"""Companion to `scripts.seed`: back-fills GPS fixes in Elasticsearch for the
trips it creates, so `GET /track/bus/{bus_id}/history` has something real to
return right after a fresh seed.

Why this is separate from `scripts.seed`
-----------------------------------------
`scripts.seed` only touches Postgres (users, buses, stops, routes, trips,
reports, alerts) — nothing in that file talks to Elasticsearch. GPS points
were deliberately dropped from Postgres (see alembic `b7f3c1a9d2e4`) and live
in ES only, so a fresh seed gives you real trips with zero history until
something like this runs.

Usage (after `python -m scripts.seed`):
    docker compose exec api python -m scripts.dev_seed_gps_for_trips

Targets Trip 2 and Trip 3 from `scripts.seed` — the two *completed* trips,
because they have a fixed actual_start/actual_end window, unlike Trip 1 which
is still "live" (open-ended). Safe to rerun: fixed `_id`s mean reruns
overwrite the same docs instead of duplicating them.
"""

import asyncio
import math
import random

from sqlalchemy import select

from app.core.elasticsearch import GPS_INDEX, ensure_gps_index, get_es_client
from app.db.session import SessionLocal
from app.models.fleet import Route, RouteStop, Stop, Trip, TripStatus

FIX_INTERVAL_S = 15  # one fix every 15s along the route


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _build_path(stops, start_ts, end_ts) -> list[dict]:
    total_s = (end_ts - start_ts).total_seconds()
    leg_s = total_s / max(len(stops) - 1, 1)
    points, t = [], start_ts
    for a, b in zip(stops, stops[1:]):
        steps = max(int(leg_s // FIX_INTERVAL_S), 1)
        heading = _bearing(a.lat, a.lng, b.lat, b.lng)
        avg_speed = _haversine(a.lat, a.lng, b.lat, b.lng) / (leg_s / 3600)
        for i in range(steps):
            frac = i / steps
            points.append(
                {
                    "ts": t,
                    "lat": _lerp(a.lat, b.lat, frac),
                    "lng": _lerp(a.lng, b.lng, frac),
                    "speed": max(avg_speed + random.uniform(-3, 3), 0),
                    "heading": heading,
                    "accuracy": random.uniform(4, 12),
                }
            )
            t += __import__("datetime").timedelta(seconds=FIX_INTERVAL_S)
    return points


async def _seed_trip(es, db, trip: Trip) -> int:
    stmt = (
        select(Stop)
        .join(RouteStop, RouteStop.stop_id == Stop.id)
        .where(RouteStop.route_id == trip.route_id)
        .order_by(RouteStop.seq)
    )
    stops = (await db.execute(stmt)).scalars().all()
    if len(stops) < 2 or trip.actual_end is None:
        print(f"  skip {trip.id} — no stops or no actual_end")
        return 0

    path = _build_path(stops, trip.actual_start, trip.actual_end)
    docs = [
        {
            "_index": GPS_INDEX,
            "_id": f"{trip.id}-{i}",  # stable -> reruns overwrite, not duplicate
            "_source": {
                "bus_id": str(trip.bus_id),
                "helper_id": str(trip.helper_id),
                "trip_id": str(trip.id),
                "ts": p["ts"].isoformat(),
                "location": {"lat": p["lat"], "lon": p["lng"]},
                "speed": round(p["speed"], 1),
                "heading": round(p["heading"], 1),
                "accuracy": round(p["accuracy"], 1),
            },
        }
        for i, p in enumerate(path)
    ]
    from elasticsearch.helpers import async_bulk

    await async_bulk(es, docs)
    return len(docs)


async def main() -> None:
    es = get_es_client()
    await ensure_gps_index(es)

    async with SessionLocal() as db:
        trips = (
            (await db.execute(select(Trip).where(Trip.status == TripStatus.completed)))
            .scalars()
            .all()
        )
        if not trips:
            print("No completed trips found — run `python -m scripts.seed` first.")
            return

        print(f"Found {len(trips)} completed trip(s):")
        for trip in trips:
            n = await _seed_trip(es, db, trip)
            print(f"  trip={trip.id}  bus={trip.bus_id}  -> indexed {n} GPS points")

    await es.indices.refresh(index=GPS_INDEX)
    print("\nDone. Use one of the trips above to test /track/bus/{bus_id}/history:")
    for trip in trips:
        print(
            f"\n  bus_id={trip.bus_id}\n"
            f"  trip_id={trip.id}\n"
            f"  from_timestamp={trip.actual_start.isoformat()}\n"
            f"  to_timestamp={trip.actual_end.isoformat()}"
        )


if __name__ == "__main__":
    asyncio.run(main())
