from redis.asyncio import Redis

from app.core.config import settings

_client: Redis | None = None

# How long the GPS indexer parks in XREADGROUP waiting for fixes.
GPS_BLOCK_MS = 5000

# redis-py 8 defaults socket_timeout to 5s, which a blocking read is measured
# against. At the default it ties GPS_BLOCK_MS exactly, so an idle stream raises
# TimeoutError and kills the consumer. Stay clear of the block window.
SOCKET_TIMEOUT_S = GPS_BLOCK_MS / 1000 + 5


def get_redis_client() -> Redis:
    """Process-wide async Redis client (decoded str responses)."""
    global _client
    if _client is None:
        _client = Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            # None when unset, which is exactly the dev default (no auth). In
            # production Redis runs with requirepass and this carries it, so the
            # addition is invisible to the local setup.
            password=settings.redis_password or None,
            decode_responses=True,
            socket_timeout=SOCKET_TIMEOUT_S,
        )
    return _client


async def get_redis() -> Redis:
    """FastAPI dependency."""
    return get_redis_client()


# --- keyspace helpers (spec §6 Redis keyspace) ---

GPS_STREAM = "gps_ingest"
# ES is the sole GPS store; its indexer is the only consumer group on the stream.
GPS_ES_CONSUMER_GROUP = "es_indexers"


def bus_pos_key(bus_id: str) -> str:
    return f"bus:{bus_id}:pos"


def fleet_channel() -> str:
    return "fleet:ch"


def bus_seats_key(bus_id: str) -> str:
    """Latest occupancy for the live map (spec §6 keyspace). TTL 900 s.

    Redis is "now": a seat count older than fifteen minutes is not current
    availability, so it expires rather than misleading a waiting student.
    Postgres keeps the full history.
    """
    return f"bus:{bus_id}:seats"


SEATS_TTL_S = 900


def alerts_channel() -> str:
    """New/updated alerts fan out to the admin console."""
    return "alerts:ch"


def helper_trip_key(helper_id: str) -> str:
    """The helper's live trip, cached so GPS ingest never queries Postgres.

    Ingest runs every 5 s per bus and needs one fact: which trip do these fixes
    belong to. Reading it from Redis keeps the hottest write path in the system
    free of database round trips.
    """
    return f"helper:{helper_id}:trip"


# A trip longer than this is abandoned, not running — a helper who force-closed
# the app mid-route. The key expiring stops a stale trip from silently
# collecting fixes for days; the row in Postgres stays the source of truth.
ACTIVE_TRIP_TTL_S = 16 * 60 * 60


def trip_eta_key(trip_id: str) -> str:
    """Precomputed arrival times for one live trip (spec §7.4).

    Written by the ETA engine once a minute, read on every student request. The
    answer depends on the bus, not the asker, so a hundred people watching one
    stop share one computation.
    """
    return f"trip:{trip_id}:eta"


def trip_progress_key(trip_id: str) -> str:
    """How far along its route a trip has got, carried between ETA passes.

    Cached rather than recomputed because progress must only ever move forward:
    an out-and-back route passes stops it already served, and re-deriving from
    the current position alone would rewind it.
    """
    return f"trip:{trip_id}:progress"


# Outlives a couple of missed ETA passes without outliving the trip. Expiry
# rather than deletion on trip end is deliberate: a trip that ends uncleanly
# would otherwise leave arrival times on the board forever.
ETA_TTL_S = 30 * 60
