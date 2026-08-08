"""ETA engine (spec §7.4) — the job that keeps arrival times fresh.

`app/services/eta.py` decides *what* an arrival time is, from plain values. This
gathers those values for every live trip and writes the answers to Redis, where
a student's request can read them without touching Postgres or Elasticsearch.

Why precomputed rather than on demand
-------------------------------------
Answering "when does the bus reach my stop" from scratch costs a route query, a
fix-history query and a distance walk. A stop can be watched by a hundred
students refreshing every few seconds, and the answer is identical for all of
them — it depends on the bus, not the asker. Computing it once per trip per
cycle turns that into a single Redis GET on the read path.

The cycle is short enough that an estimate is never badly stale and long enough
that the fleet does not hammer Elasticsearch. Progress and delay are carried
between passes in Redis: a bus's furthest-reached stop is a fact about the trip,
and recomputing it from scratch each time is both wasteful and — because
progress must never move backwards — wrong.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.elasticsearch import GPS_INDEX, get_es_client
from app.core.redis import (
    ETA_TTL_S,
    get_redis_client,
    trip_eta_key,
    trip_progress_key,
)
from app.db.session import SessionLocal
from app.models.fleet import Route, RouteStop, Trip, TripStatus
from app.services.eta import (
    Fix,
    Point,
    Progress,
    RoutePoint,
    advance_progress,
    estimate_arrivals,
    rolling_speed_kmh,
)

logger = logging.getLogger("unitrack.worker.eta")

# Every 60 s. The helper batches GPS every ~5 s, so this is far coarser than the
# data — but an arrival time that moves every five seconds reads as jitter, not
# precision, and the cost is a query per live trip.
INTERVAL_S = 60

# How much fix history feeds the speed calculation. Long enough to average out
# a traffic light, short enough to still be about now.
SPEED_WINDOW_S = 5 * 60
FIX_LIMIT = 200


async def _live_trips(db) -> list[Trip]:
    """Live trips with their route's stops already loaded.

    `selectinload` rather than the lazy default: this runs every minute over the
    whole live fleet, and walking relationships per trip would turn one query
    into one per stop per bus.
    """
    stmt = (
        select(Trip)
        .where(Trip.status == TripStatus.live)
        .options(
            selectinload(Trip.route)
            .selectinload(Route.stops)
            .selectinload(RouteStop.stop)
        )
    )
    return list((await db.execute(stmt)).scalars())


async def _recent_fixes(es, trip_id: str, now: datetime) -> list[Fix]:
    """This trip's fixes from the last few minutes, oldest first."""
    try:
        result = await es.search(
            index=GPS_INDEX,
            size=FIX_LIMIT,
            query={
                "bool": {
                    "filter": [
                        {"term": {"trip_id": trip_id}},
                        {"range": {"ts": {"gte": f"now-{SPEED_WINDOW_S}s"}}},
                    ]
                }
            },
            sort=[{"ts": {"order": "asc"}}],
        )
    except Exception:  # noqa: BLE001 — no fixes is a degraded answer, not a crash
        logger.warning("could not read fixes for trip %s", trip_id)
        return []

    fixes: list[Fix] = []
    for hit in result["hits"]["hits"]:
        source = hit["_source"]
        location = source.get("location") or {}
        try:
            fixes.append(
                Fix(
                    lat=float(location["lat"]),
                    lng=float(location["lon"]),
                    ts=datetime.fromisoformat(source["ts"]).astimezone(UTC),
                )
            )
        except (KeyError, TypeError, ValueError):
            # One malformed document must not cost the whole trip its estimate.
            continue
    return fixes


def _route_points(trip: Trip) -> list[RoutePoint]:
    return [
        RoutePoint(
            stop_id=str(rs.stop_id),
            seq=rs.seq,
            lat=rs.stop.lat,
            lng=rs.stop.lng,
            scheduled_offset_min=rs.scheduled_offset_min,
        )
        for rs in trip.route.stops
    ]


async def _load_progress(r, trip_id: str) -> Progress | None:
    try:
        raw = await r.get(trip_progress_key(trip_id))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return Progress(seq=int(data["seq"]), at=datetime.fromisoformat(data["at"]))
    except (ValueError, KeyError, TypeError):
        return None


async def _save_progress(r, trip_id: str, progress: Progress) -> None:
    payload = json.dumps({"seq": progress.seq, "at": progress.at.isoformat()})
    try:
        await r.set(trip_progress_key(trip_id), payload, ex=ETA_TTL_S)
    except Exception:  # noqa: BLE001
        logger.warning("could not persist progress for trip %s", trip_id)


async def compute_for_trip(r, es, trip: Trip, now: datetime) -> int:
    """Estimate and cache one trip's arrivals. Returns how many were written."""
    trip_id = str(trip.id)
    points = _route_points(trip)
    if not points or trip.actual_start is None:
        return 0

    fixes = await _recent_fixes(es, trip_id, now)
    position = Point(fixes[-1].lat, fixes[-1].lng) if fixes else None
    speed = rolling_speed_kmh(fixes, now)

    progress = await _load_progress(r, trip_id)
    if fixes:
        # The whole window, not just the latest fix: at 25 km/h a bus covers
        # 400 m between passes and would otherwise drive past a stop unseen.
        advanced = advance_progress(points, fixes, progress)
        if advanced is not None and advanced is not progress:
            await _save_progress(r, trip_id, advanced)
        progress = advanced

    arrivals = estimate_arrivals(
        points,
        position=position,
        progress=progress,
        actual_start=trip.actual_start.astimezone(UTC),
        speed_kmh=speed,
        now=now,
    )
    if not arrivals:
        return 0

    payload = json.dumps(
        {
            "trip_id": trip_id,
            "route_id": str(trip.route_id),
            "bus_id": str(trip.bus_id),
            "computed_at": now.isoformat(),
            "arrivals": [
                {
                    "stop_id": a.stop_id,
                    "seq": a.seq,
                    "eta": a.eta.isoformat(),
                    "eta_minutes": max(round((a.eta - now).total_seconds() / 60), 0),
                    "basis": a.basis,
                    "distance_km": a.distance_km,
                }
                for a in arrivals
            ],
        }
    )
    try:
        # TTL rather than an explicit delete on trip end: a trip that ends
        # uncleanly — a crashed worker, a helper who force-closed the app —
        # would otherwise leave arrival times on the board forever.
        await r.set(trip_eta_key(trip_id), payload, ex=ETA_TTL_S)
    except Exception:  # noqa: BLE001
        logger.warning("could not cache ETAs for trip %s", trip_id)
        return 0

    return len(arrivals)


async def compute_once() -> dict[str, int]:
    """One pass over the live fleet. Returns a tally worth logging."""
    tally = {"trips": 0, "estimated": 0, "arrivals": 0}
    now = datetime.now(UTC)
    r = get_redis_client()
    es = get_es_client()

    async with SessionLocal() as db:
        trips = await _live_trips(db)

    for trip in trips:
        tally["trips"] += 1
        try:
            written = await compute_for_trip(r, es, trip, now)
        except Exception:  # noqa: BLE001
            # One bad trip — a route whose stops were edited mid-journey, a
            # malformed fix — must not cost the rest of the fleet its estimates.
            logger.exception("ETA failed for trip %s; continuing", trip.id)
            continue
        if written:
            tally["estimated"] += 1
            tally["arrivals"] += written

    return tally


async def run() -> None:
    logger.info("ETA engine running every %ds", INTERVAL_S)
    while True:
        try:
            tally = await compute_once()
            if tally["trips"]:
                logger.info("ETA pass: %s", tally)
        except Exception:  # noqa: BLE001
            # An engine that dies stops answering the one question students
            # actually ask, and nothing else would notice it was gone.
            logger.exception("ETA pass failed; continuing")
        await asyncio.sleep(INTERVAL_S)
