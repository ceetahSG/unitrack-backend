"""What makes an arrival time trustworthy, and what makes one a lie.

An ETA is read by someone deciding whether to leave the building. So the tests
that matter are not "does it return a number" — it is easy to always return a
number — but the cases where a plausible-looking number would be wrong:

- a bus stopped at a light, whose current speed says "four hours"
- a GPS jump, whose current speed says "ninety seconds"
- a route that doubles back past a stop it already served
- two estimators disagreeing at the seam, so the board counts downwards

Every one of those produces confident nonsense if unguarded.
"""

from datetime import UTC, datetime, timedelta

from app.services.eta import (
    MAX_HORIZON,
    Fix,
    Point,
    Progress,
    RoutePoint,
    advance_progress,
    estimate_arrivals,
    haversine_km,
    rolling_speed_kmh,
    schedule_delay,
)

NOW = datetime(2026, 8, 7, 8, 0, tzinfo=UTC)
START = NOW - timedelta(minutes=20)

# A straight run north along one line of longitude. Each step is ~1.11 km, which
# makes the arithmetic checkable by hand.
ROUTE = [
    RoutePoint(stop_id="a", seq=1, lat=23.74, lng=90.40, scheduled_offset_min=0),
    RoutePoint(stop_id="b", seq=2, lat=23.75, lng=90.40, scheduled_offset_min=10),
    RoutePoint(stop_id="c", seq=3, lat=23.76, lng=90.40, scheduled_offset_min=20),
    RoutePoint(stop_id="d", seq=4, lat=23.77, lng=90.40, scheduled_offset_min=30),
]


def _fixes(*offsets_and_lats: tuple[int, float]) -> list[Fix]:
    """Fixes at (seconds before NOW, latitude) along the route's longitude."""
    return [
        Fix(lat=lat, lng=90.40, ts=NOW - timedelta(seconds=secs))
        for secs, lat in offsets_and_lats
    ]


# --- distance ---------------------------------------------------------------


def test_haversine_matches_known_distance() -> None:
    """0.01 degrees of latitude is ~1.11 km anywhere on earth."""
    km = haversine_km(Point(23.74, 90.40), Point(23.75, 90.40))
    assert 1.10 < km < 1.12


# --- speed ------------------------------------------------------------------


def test_speed_is_distance_over_total_elapsed_not_an_average_of_pairs() -> None:
    """Time spent stationary has to count.

    This bus covers ~1.11 km in 10 minutes, most of it in the first two — a
    light, then a queue. It is doing about 6.7 km/h. Averaging the per-pair
    speeds would report the sprint and promise an arrival it cannot make.
    """
    fixes = _fixes((600, 23.7400), (540, 23.7450), (480, 23.7500), (0, 23.7500))
    speed = rolling_speed_kmh(fixes, NOW)
    assert speed is not None
    assert 6.0 < speed < 7.5


def test_a_bus_at_a_standstill_has_no_usable_speed() -> None:
    """Returning 0 would be arithmetically true and operationally a disaster —
    the caller divides by it. None forces the fall back to the schedule."""
    assert rolling_speed_kmh(_fixes((600, 23.74), (0, 23.7401)), NOW) is None


def test_a_gps_jump_is_rejected_rather_than_believed() -> None:
    """20 km in 60 s is 1200 km/h. Something is wrong with the fix, not the bus."""
    assert rolling_speed_kmh(_fixes((60, 23.74), (0, 23.92)), NOW) is None


def test_a_bus_that_has_gone_quiet_has_no_current_speed() -> None:
    """Fixes stop when a phone dies or enters a tunnel. What it was doing ten
    minutes ago is history, not evidence."""
    stale = [
        Fix(lat=23.74, lng=90.40, ts=NOW - timedelta(minutes=20)),
        Fix(lat=23.75, lng=90.40, ts=NOW - timedelta(minutes=10)),
    ]
    assert rolling_speed_kmh(stale, NOW) is None


def test_a_single_fix_is_not_a_speed() -> None:
    assert rolling_speed_kmh(_fixes((0, 23.74)), NOW) is None


# --- progress ---------------------------------------------------------------


def test_arriving_at_a_stop_moves_progress_forward() -> None:
    progress = advance_progress(ROUTE, _fixes((0, 23.7500)), None)
    assert progress is not None
    assert progress.seq == 2


def test_a_stop_passed_between_engine_passes_is_still_detected() -> None:
    """The engine runs once a minute; a bus at 25 km/h covers 400 m in that
    time. Looking only at where it is *now* misses every stop it drove past,
    and a trip whose progress never advances keeps reporting arrivals for
    stops it already served."""
    drove_past = _fixes((90, 23.7480), (60, 23.7500), (30, 23.7530), (0, 23.7560))
    progress = advance_progress(ROUTE, drove_past, None)
    assert progress is not None
    assert progress.seq == 2


def test_progress_is_stamped_with_the_fix_time_not_the_engine_clock() -> None:
    """`schedule_delay` subtracts this from the scheduled offset, so using the
    engine's own clock would fold up to a minute of scheduling latency into the
    measured delay and push every downstream estimate out with it."""
    passed_at = NOW - timedelta(seconds=45)
    progress = advance_progress(ROUTE, _fixes((45, 23.7500), (0, 23.7530)), None)
    assert progress is not None
    assert progress.at == passed_at


def test_progress_never_goes_backwards() -> None:
    """An out-and-back shares roads, so a bus genuinely passes stops it already
    served. Rewinding would make every later ETA jump."""
    at_third = Progress(seq=3, at=NOW - timedelta(minutes=5))
    unchanged = advance_progress(ROUTE, _fixes((0, 23.7400)), at_third)
    assert unchanged is at_third


def test_a_skipped_stop_still_counts_as_passed() -> None:
    """Fixes arrive every few seconds but a batch can be lost. If the bus is
    now at stop 4, it did not teleport — stops 2 and 3 are behind it."""
    progress = advance_progress(ROUTE, _fixes((0, 23.7700)), None)
    assert progress is not None
    assert progress.seq == 4


def test_being_near_no_stop_leaves_progress_alone() -> None:
    assert advance_progress(ROUTE, _fixes((0, 23.7455)), None) is None


# --- delay ------------------------------------------------------------------


def test_delay_is_measured_from_the_last_stop_reached() -> None:
    """Reached stop 2 (scheduled +10 min) 16 minutes in: six minutes late."""
    progress = Progress(seq=2, at=START + timedelta(minutes=16))
    assert schedule_delay(ROUTE, progress, START) == timedelta(minutes=6)


def test_a_bus_that_has_reached_nothing_yet_is_not_assumed_late() -> None:
    assert schedule_delay(ROUTE, None, START) == timedelta()


def test_an_unscheduled_stop_yields_no_delay_rather_than_a_guess() -> None:
    """`scheduled_offset_min` is nullable and often unset. Treating a missing
    offset as 0 would report a bus as catastrophically late."""
    unscheduled = [RoutePoint(stop_id="a", seq=1, lat=23.74, lng=90.40)]
    progress = Progress(seq=1, at=START + timedelta(minutes=30))
    assert schedule_delay(unscheduled, progress, START) == timedelta()


# --- arrivals ---------------------------------------------------------------


def test_the_next_stops_use_observed_speed_and_say_so() -> None:
    arrivals = estimate_arrivals(
        ROUTE,
        position=Point(23.7450, 90.40),
        progress=Progress(seq=1, at=START),
        actual_start=START,
        speed_kmh=20.0,
        now=NOW,
    )
    assert [a.stop_id for a in arrivals] == ["b", "c", "d"]
    assert arrivals[0].basis == "live"


def test_distant_stops_fall_back_to_the_schedule() -> None:
    """Current speed says nothing useful about a stop forty minutes away."""
    arrivals = estimate_arrivals(
        ROUTE,
        position=Point(23.7450, 90.40),
        progress=Progress(seq=1, at=START),
        actual_start=START,
        speed_kmh=20.0,
        now=NOW,
    )
    assert arrivals[-1].basis == "scheduled"


def test_a_stopped_bus_still_gets_scheduled_arrivals() -> None:
    """No usable speed is the common case in Dhaka traffic, not an edge case.
    Answering nothing at all would make the feature useless exactly when it is
    most wanted."""
    arrivals = estimate_arrivals(
        ROUTE,
        position=Point(23.7450, 90.40),
        progress=Progress(seq=1, at=START),
        actual_start=START,
        speed_kmh=None,
        now=NOW,
    )
    assert arrivals
    assert {a.basis for a in arrivals} == {"scheduled"}


def test_measured_delay_pushes_every_scheduled_arrival_out() -> None:
    """The free path's whole traffic-awareness claim.

    No traffic feed is consulted. A bus six minutes late at stop 2 is telling
    you what the traffic did, and stop 4 inherits it.
    """
    on_time = estimate_arrivals(
        ROUTE,
        position=None,
        progress=Progress(seq=2, at=START + timedelta(minutes=10)),
        actual_start=START,
        speed_kmh=None,
        now=NOW,
    )
    late = estimate_arrivals(
        ROUTE,
        position=None,
        progress=Progress(seq=2, at=START + timedelta(minutes=16)),
        actual_start=START,
        speed_kmh=None,
        now=NOW,
    )
    last_on_time = next(a for a in on_time if a.stop_id == "d")
    last_late = next(a for a in late if a.stop_id == "d")
    assert last_late.eta - last_on_time.eta == timedelta(minutes=6)


def test_arrival_times_never_count_downwards_along_the_route() -> None:
    """The two estimators disagree at the seam.

    A crawling bus gets a pessimistic "live" estimate for the next stop that can
    land after the "scheduled" estimate for the one beyond it. A board reading
    `Banani 18 min, Mohakhali 12 min` is visibly broken.
    """
    arrivals = estimate_arrivals(
        ROUTE,
        position=Point(23.7410, 90.40),
        progress=Progress(seq=1, at=START),
        actual_start=START,
        # Just above the usable floor: slow enough that the near-stop estimate
        # overshoots the scheduled ones behind it.
        speed_kmh=3.5,
        now=NOW,
    )
    times = [a.eta for a in arrivals]
    assert times == sorted(times)


def test_stops_already_served_are_not_reported() -> None:
    arrivals = estimate_arrivals(
        ROUTE,
        position=Point(23.7600, 90.40),
        progress=Progress(seq=3, at=NOW),
        actual_start=START,
        speed_kmh=20.0,
        now=NOW,
    )
    assert [a.stop_id for a in arrivals] == ["d"]


def test_an_arrival_in_the_past_is_reported_as_now() -> None:
    """A bus far later than the measured delay caught would otherwise be shown
    arriving at a time that has already been and gone."""
    arrivals = estimate_arrivals(
        ROUTE,
        position=None,
        progress=Progress(seq=1, at=START),
        actual_start=NOW - timedelta(hours=2),
        speed_kmh=None,
        now=NOW,
    )
    assert all(a.eta >= NOW for a in arrivals)


def test_estimates_beyond_the_horizon_are_dropped_not_shown() -> None:
    """"Arriving in four hours" is not information about when to leave."""
    far = [RoutePoint(stop_id="z", seq=1, lat=23.74, lng=90.40, scheduled_offset_min=600)]
    arrivals = estimate_arrivals(
        far,
        position=None,
        progress=None,
        actual_start=NOW,
        speed_kmh=None,
        now=NOW,
    )
    assert arrivals == []
    assert MAX_HORIZON < timedelta(minutes=600)


def test_a_finished_route_reports_nothing() -> None:
    arrivals = estimate_arrivals(
        ROUTE,
        position=Point(23.77, 90.40),
        progress=Progress(seq=4, at=NOW),
        actual_start=START,
        speed_kmh=20.0,
        now=NOW,
    )
    assert arrivals == []


def test_no_speed_and_no_schedule_produces_no_estimate() -> None:
    """The one case where silence is the only honest answer."""
    unscheduled = [
        RoutePoint(stop_id="a", seq=1, lat=23.75, lng=90.40),
        RoutePoint(stop_id="b", seq=2, lat=23.76, lng=90.40),
    ]
    arrivals = estimate_arrivals(
        unscheduled,
        position=Point(23.74, 90.40),
        progress=None,
        actual_start=START,
        speed_kmh=None,
        now=NOW,
    )
    assert arrivals == []


def test_distance_accumulates_along_the_route_not_as_the_crow_flies() -> None:
    """Measuring each stop from the current position would let a later stop on
    a winding route look nearer than an earlier one."""
    # Short of the first stop, not sitting on it — a bus exactly at a stop is
    # zero away from it, which would make the assertion vacuous.
    arrivals = estimate_arrivals(
        ROUTE,
        position=Point(23.7390, 90.40),
        progress=None,
        actual_start=START,
        speed_kmh=20.0,
        now=NOW,
    )
    distances = [a.distance_km for a in arrivals]
    assert distances == sorted(distances)
    assert distances[0] > 0
    # Four stops ~1.11 km apart, plus the road factor: the last is several km
    # out, not the ~4.4 km a straight line would claim.
    assert distances[-1] > 4.4
