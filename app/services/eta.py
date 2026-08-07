"""When will the bus get here (spec §7.4).

The only question a student standing at a stop actually has. The live map
answers "where is it", which is not the same thing — a bus two kilometres away
on an empty road and one two kilometres away in Farmgate traffic are twenty
minutes apart.

Two estimators, because neither is sufficient alone
---------------------------------------------------
**Observed speed.** Distance to the stop divided by how fast the bus has
actually been moving. Good for the next stop or two. Useless further out: the
bus is not going to hold this minute's speed for the next forty.

**Schedule plus measured delay.** `route_stops.scheduled_offset_min` says the
bus should reach stop 5 at start + 40 min. If it reached stop 3 six minutes
late, stop 5 is probably about six minutes late too. Good for the whole
remaining route, and it degrades gracefully — a bus with no recent fix still
has a defensible answer.

So the near stops use speed and the far ones use the schedule, and every
estimate says which one produced it. That last part matters: "3 min (live)"
and "3 min (scheduled)" deserve different amounts of trust from whoever is
deciding whether to run for it.

Why speed is derived here rather than read from the GPS fix
-----------------------------------------------------------
`GpsPointIn.speed` comes from the device, and the helper app fills it from
geolocator's `Position.speed`, which is **metres per second** — a fact recorded
in no schema, no comment and no test. Multiplying that by the wrong constant is
a silent 3.6x error in every arrival time, and nothing about the output would
look obviously wrong.

Distance between two fixes divided by the time between them has no such
ambiguity: this module computes the kilometres and reads the seconds, so the
unit is whatever this module says it is. It is also the more useful number —
ground speed made good along a route, including the time spent stationary at
lights, which is exactly what an arrival time depends on.

Nothing here touches the database, Elasticsearch or the clock beyond what it is
handed, so the whole thing is testable with plain values.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Straight lines between stops underestimate the road. A bus does not fly from
# Banani to Mohakhali; it follows a road that bends. 1.3 is the usual rough
# correction for urban networks and is roughly right for Dhaka's grid.
#
# It is a constant rather than a measurement because the honest measurement —
# `routes.polyline` — is nullable and mostly unset. When polylines are filled
# in, replace this with real distance along the line and delete the constant.
ROAD_FACTOR = 1.3

# Below this, "speed" is a bus at a light, not a bus in motion. Dividing by it
# yields an arrival time of several hours, which is worse than no answer: it
# looks authoritative and is nonsense.
MIN_USABLE_KMH = 3.0

# Above this, the fix history is lying — a GPS jump, a device clock that moved,
# or the helper's phone in a car on a flyover. Capping stops one bad pair of
# fixes from promising an arrival that cannot happen.
MAX_PLAUSIBLE_KMH = 80.0

# How far ahead observed speed is allowed to speak for. Beyond a couple of
# stops, current speed says more about this traffic light than about the rest
# of the route.
SPEED_HORIZON_STOPS = 2

# Estimates beyond this are not information. A route that says "arriving in
# 4 hours" is telling you something has gone wrong, not when to leave.
MAX_HORIZON = timedelta(hours=3)

# A fix older than this is not evidence of current speed.
STALE_FIX_S = 5 * 60

EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True, slots=True)
class Point:
    lat: float
    lng: float


@dataclass(frozen=True, slots=True)
class Fix:
    """One GPS observation. `ts` must be timezone-aware."""

    lat: float
    lng: float
    ts: datetime


@dataclass(frozen=True, slots=True)
class RoutePoint:
    """One stop in a route's order, with its scheduled offset if anyone set one."""

    stop_id: str
    seq: int
    lat: float
    lng: float
    scheduled_offset_min: int | None = None


@dataclass(frozen=True, slots=True)
class Progress:
    """How far along the route the bus has got, and when it got there.

    Held per trip rather than recomputed, because it must only ever move
    forward. A bus that loops back within GPS error of an earlier stop has not
    un-visited the stops between, and an ETA that jumps backwards is worse than
    a stale one.
    """

    seq: int
    at: datetime


@dataclass(frozen=True, slots=True)
class Arrival:
    stop_id: str
    seq: int
    eta: datetime
    # "live" — derived from how fast the bus is actually moving.
    # "scheduled" — the timetable, shifted by the delay measured so far.
    # Shown to the student, because they mean different things.
    basis: str
    distance_km: float


def haversine_km(a: Point, b: Point) -> float:
    """Great-circle distance. Flat-earth maths is wrong enough at Dhaka's
    latitude to matter over a 20 km route."""
    lat1, lng1, lat2, lng2 = map(math.radians, (a.lat, a.lng, b.lat, b.lng))
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def rolling_speed_kmh(fixes: list[Fix], now: datetime) -> float | None:
    """Ground speed made good, from the fix history. None when unusable.

    Total distance over total elapsed time, not an average of per-pair speeds:
    the time a bus spends stationary has to count, and averaging pairs quietly
    discards it. A bus that covers 2 km in 20 minutes is doing 6 km/h whatever
    it managed in the fastest thirty seconds of that.

    Returns None rather than 0 for a stopped bus, so callers cannot divide by it
    and must fall back to the schedule.
    """
    if len(fixes) < 2:
        return None

    ordered = sorted(fixes, key=lambda f: f.ts)
    newest = ordered[-1]
    if (now - newest.ts).total_seconds() > STALE_FIX_S:
        # The bus has gone quiet — a tunnel, a dead battery, a helper who closed
        # the app. Whatever it was doing five minutes ago is not evidence now.
        return None

    elapsed_h = (newest.ts - ordered[0].ts).total_seconds() / 3600
    if elapsed_h <= 0:
        return None

    distance = sum(
        haversine_km(Point(a.lat, a.lng), Point(b.lat, b.lng))
        for a, b in zip(ordered, ordered[1:], strict=False)
    )
    speed = distance / elapsed_h

    if speed < MIN_USABLE_KMH or speed > MAX_PLAUSIBLE_KMH:
        return None
    return speed


def advance_progress(
    points: list[RoutePoint],
    fixes: list[Fix],
    previous: Progress | None,
    *,
    arrival_radius_km: float = 0.15,
) -> Progress | None:
    """Move the trip's progress forward over the whole fix window.

    Takes the fix *history*, not just the current position, and that is the
    whole point. The engine runs once a minute; a bus at 25 km/h covers 400 m
    between passes, so checking only where it is right now misses every stop it
    drove past in between — and a route whose progress never advances reports
    arrivals for stops it already served.

    `at` is the timestamp of the fix that reached the stop, not the time this
    ran. `schedule_delay` subtracts it from the scheduled offset, so using the
    engine's own clock would fold up to a minute of scheduling latency into a
    delay measurement and push every downstream estimate out with it.

    Only ever forward. A route that passes near an earlier stop — common on an
    out-and-back sharing a road — must not rewind the whole estimate.

    150 m is roughly "the bus is at this stop" once consumer GPS error and the
    width of a Dhaka road are accounted for. Tighter misses arrivals; looser
    starts claiming stops on the other side of an intersection.
    """
    reached = previous.seq if previous else 0

    best = previous
    for fix in sorted(fixes, key=lambda f: f.ts):
        here = Point(fix.lat, fix.lng)
        for point in points:
            if point.seq <= (best.seq if best else 0):
                continue
            if haversine_km(here, Point(point.lat, point.lng)) <= arrival_radius_km:
                # Keep scanning rather than breaking: a sparse fix stream can
                # put two stops inside one window, and the furthest one reached
                # is the truthful answer.
                best = Progress(seq=point.seq, at=fix.ts)

    if best is not None and best.seq == reached:
        return previous
    return best


def schedule_delay(
    points: list[RoutePoint], progress: Progress | None, actual_start: datetime
) -> timedelta:
    """How late the bus was at the last stop it reached. Zero if unknowable.

    This is the whole "traffic-aware" claim of the free path: no traffic feed is
    consulted, but a bus six minutes late at stop 3 is telling you what the
    traffic did to it, and that is the best available predictor of stop 4.
    """
    if progress is None or progress.seq <= 0:
        return timedelta()

    scheduled = next(
        (p.scheduled_offset_min for p in points if p.seq == progress.seq),
        None,
    )
    if scheduled is None:
        return timedelta()

    actual_elapsed = progress.at - actual_start
    return actual_elapsed - timedelta(minutes=scheduled)


def _remaining(points: list[RoutePoint], reached: int) -> list[RoutePoint]:
    return sorted((p for p in points if p.seq > reached), key=lambda p: p.seq)


def estimate_arrivals(
    points: list[RoutePoint],
    *,
    position: Point | None,
    progress: Progress | None,
    actual_start: datetime,
    speed_kmh: float | None,
    now: datetime | None = None,
) -> list[Arrival]:
    """Arrival times for every stop the bus has not reached yet.

    Returns an empty list when there is nothing defensible to say — no remaining
    stops, or neither a usable speed nor a schedule to fall back on. An empty
    answer is a real answer; a fabricated one is not.
    """
    now = now or datetime.now(UTC)
    remaining = _remaining(points, progress.seq if progress else 0)
    if not remaining:
        return []

    delay = schedule_delay(points, progress, actual_start)

    # Cumulative road distance from the bus to each remaining stop: out to the
    # next stop, then stop to stop. Measuring each stop directly from the
    # current position would have a winding route's later stops appear closer
    # than its nearer ones.
    distances: dict[int, float] = {}
    if position is not None:
        running = 0.0
        cursor = position
        for point in remaining:
            running += haversine_km(cursor, Point(point.lat, point.lng)) * ROAD_FACTOR
            distances[point.seq] = running
            cursor = Point(point.lat, point.lng)

    arrivals: list[Arrival] = []
    for index, point in enumerate(remaining):
        distance = distances.get(point.seq, 0.0)

        eta: datetime | None = None
        basis = ""

        # Near stops: what the bus is actually doing right now.
        if speed_kmh and index < SPEED_HORIZON_STOPS and point.seq in distances:
            eta = now + timedelta(hours=distance / speed_kmh)
            basis = "live"

        # Everything else: the timetable, shifted by the delay measured so far.
        if eta is None and point.scheduled_offset_min is not None:
            eta = actual_start + timedelta(minutes=point.scheduled_offset_min) + delay
            basis = "scheduled"

        # A schedule-based estimate already in the past means the bus is late
        # past what the delay measurement caught. Reporting a time that has
        # been and gone is worse than saying "about now".
        if eta is not None and eta < now:
            eta = now

        if eta is None or eta - now > MAX_HORIZON:
            continue

        arrivals.append(
            Arrival(
                stop_id=point.stop_id,
                seq=point.seq,
                eta=eta,
                basis=basis,
                distance_km=round(distance, 2),
            )
        )

    return _monotonic(arrivals)


def _monotonic(arrivals: list[Arrival]) -> list[Arrival]:
    """Force arrival times to increase along the route.

    The two estimators disagree at the seam: a bus crawling at 4 km/h produces a
    "live" estimate for stop 4 that lands after the "scheduled" estimate for
    stop 5, and a board reading *Mohakhali 18 min, Farmgate 12 min* is obviously
    broken to anyone looking at it. Later stops are pushed out rather than
    earlier ones pulled in, because the pessimistic reading is the one that does
    not make someone miss a bus.
    """
    latest: datetime | None = None
    fixed: list[Arrival] = []
    for arrival in arrivals:
        eta = arrival.eta if latest is None or arrival.eta >= latest else latest
        latest = eta
        fixed.append(
            Arrival(
                stop_id=arrival.stop_id,
                seq=arrival.seq,
                eta=eta,
                basis=arrival.basis,
                distance_km=arrival.distance_km,
            )
        )
    return fixed
