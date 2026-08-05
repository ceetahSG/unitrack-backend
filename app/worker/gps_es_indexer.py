"""GPS Elasticsearch indexer (worker job 1b).

The only consumer group on the `gps_ingest` stream: Elasticsearch is the sole
GPS store (spec §5.1 deviation — see the README). Indexes each fix as a
geo_point doc for geo_distance / viewport / heatmap queries.
"""

import asyncio
import logging

from elasticsearch.helpers import async_bulk
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.core.elasticsearch import GPS_INDEX, ensure_gps_index, get_es_client
from app.core.redis import (
    GPS_BLOCK_MS,
    GPS_ES_CONSUMER_GROUP,
    GPS_STREAM,
    get_redis_client,
)

logger = logging.getLogger("unitrack.worker.gps_es")

CONSUMER_NAME = "gps-es-1"
BATCH = 100


def _to_float(v: str) -> float | None:
    return float(v) if v not in ("", None) else None


async def _ensure_group(r) -> None:
    try:
        await r.xgroup_create(GPS_STREAM, GPS_ES_CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("Created consumer group %s on %s", GPS_ES_CONSUMER_GROUP, GPS_STREAM)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def _to_doc(entry_id: str, fields: dict) -> dict:
    # Stream carries lng; ES geo_point wants lon.
    return {
        "_index": GPS_INDEX,
        "_id": entry_id,  # stream id = idempotent doc id (reprocess-safe)
        "_source": {
            "bus_id": fields["bus_id"],
            "helper_id": fields.get("helper_id"),
            # Empty string means the fix predates the helper's trip lifecycle.
            # Index it as null so `exists`/`terms` queries treat it as absent
            # rather than matching a bogus "" trip.
            "trip_id": fields.get("trip_id") or None,
            "ts": fields["ts"],
            "location": {"lat": float(fields["lat"]), "lon": float(fields["lng"])},
            "speed": _to_float(fields.get("speed", "")),
            "heading": _to_float(fields.get("heading", "")),
            "accuracy": _to_float(fields.get("accuracy", "")),
        },
    }


async def run() -> None:
    r = get_redis_client()
    es = get_es_client()
    await _ensure_group(r)
    await ensure_gps_index(es)
    logger.info("GPS ES indexer running on stream %s", GPS_STREAM)
    while True:
        try:
            resp = await r.xreadgroup(
                GPS_ES_CONSUMER_GROUP,
                CONSUMER_NAME,
                streams={GPS_STREAM: ">"},
                count=BATCH,
                block=GPS_BLOCK_MS,
            )
        except RedisTimeoutError:
            # An idle stream is normal, and compose sets no restart policy, so a
            # transient read timeout must not be fatal.
            continue
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            # The stream or its consumer group vanished underneath us: Redis
            # restarted without persistence, the key was evicted, or a dev reset
            # flushed the database. Creating the group only at startup meant
            # this killed the indexer for good, and with no restart policy the
            # GPS pipeline stayed silently dead until someone noticed
            # /track/nearby had gone empty.
            #
            # Recreating and continuing is safe: the ES document id is the
            # stream id, so replaying entries re-indexes them in place rather
            # than duplicating them.
            logger.warning("consumer group missing (%s) — recreating and resuming", exc)
            await _ensure_group(r)
            continue
        if not resp:
            continue
        for _stream, entries in resp:
            docs = [_to_doc(eid, fields) for eid, fields in entries]
            await async_bulk(es, docs)
            await r.xack(GPS_STREAM, GPS_ES_CONSUMER_GROUP, *[eid for eid, _ in entries])
            logger.info("Indexed %d gps points to ES", len(docs))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
