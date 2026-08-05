"""Return a dev environment to a known-empty state, then reseed it.

`scripts.seed --wipe` only removes rows the seed itself created. That is the
right scope for reseeding, but it leaves everything else behind — smoke-test
accounts, hand-approved helpers, GPS fixes in Elasticsearch, cached principals
and revoked tokens in Redis. State accumulates across all three stores until
"works on my machine" stops meaning anything.

This clears all three and reseeds:

    docker compose exec api python -m scripts.reset_dev --yes

State lives in three places and all three have to go together:

- **Postgres** — every application table is truncated. `alembic_version` is
  deliberately untouched, since the schema is not the thing being reset; wiping
  it would strand the database at an unknown revision.
- **Elasticsearch** — the GPS index is dropped and recreated from the mapping
  in `app.core.elasticsearch`, so a mapping change lands here too.
- **Redis** — flushed. It holds the principal cache, the revocation denylist,
  live-trip bindings and the GPS ingest stream. Clearing Postgres while leaving
  Redis populated is worse than clearing neither: `authz:principal:*` would
  answer for users that no longer exist, and `gps_ingest` would replay fixes
  belonging to deleted trips.

DESTRUCTIVE. Refuses to run unless `ENV=dev`, and requires `--yes`; there is no
interactive prompt, because a script like this gets run from CI and Makefiles
where a missed prompt becomes a hang rather than a refusal.
"""

import argparse
import asyncio
import sys

from redis.exceptions import ResponseError
from sqlalchemy import text

from app.core.config import settings
from app.core.elasticsearch import GPS_INDEX, ensure_gps_index, get_es_client
from app.core.redis import GPS_ES_CONSUMER_GROUP, GPS_STREAM, get_redis_client
from app.db.base import Base
from app.db.session import engine

# Importing the models is what populates `Base.metadata`. Without it the
# truncate below would find no tables and silently do nothing — the worst
# possible outcome for a reset script, since it reports success.
from app.models import fleet, ops, user  # noqa: F401
from scripts.seed import SEED_ORDER, _run


async def _truncate_postgres() -> list[str]:
    """Empty every mapped table in one statement.

    Deriving the list from `Base.metadata` rather than hardcoding it means a
    table added next month is reset too, without anyone remembering to come
    here. CASCADE handles the FK graph; RESTART IDENTITY resets sequences so a
    reseeded database looks like a fresh one.
    """
    names = [f'"{t.name}"' for t in Base.metadata.sorted_tables]
    if not names:
        raise RuntimeError("no tables found in metadata — models were not imported")

    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY CASCADE"))
    return [t.name for t in Base.metadata.sorted_tables]


async def _reset_elasticsearch() -> None:
    es = get_es_client()
    try:
        await es.indices.delete(index=GPS_INDEX, ignore_unavailable=True)
        await ensure_gps_index(es)
    finally:
        await es.close()


async def _flush_redis() -> None:
    """Flush, then put back the one structure Redis is expected to already have.

    FLUSHDB takes the `gps_ingest` stream and its `es_indexers` consumer group
    with it, and a consumer group is not recreated by publishing to the stream —
    so the next `XREADGROUP` fails with NOGROUP and the GPS pipeline is dead
    until someone restarts the worker. Recreating it here leaves Redis in the
    state the rest of the system assumes.

    The worker also heals itself from NOGROUP now, so this is belt and braces;
    it means a reset does not depend on the worker noticing.
    """
    r = get_redis_client()
    await r.flushdb()
    try:
        await r.xgroup_create(GPS_STREAM, GPS_ES_CONSUMER_GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _reset() -> None:
    print("Resetting dev environment.\n")

    tables = await _truncate_postgres()
    print(f"  postgres      truncated {len(tables)} table(s): {', '.join(tables)}")

    await _reset_elasticsearch()
    print(f"  elasticsearch dropped and recreated index '{GPS_INDEX}'")

    await _flush_redis()
    print(f"  redis         flushed, consumer group '{GPS_ES_CONSUMER_GROUP}' recreated\n")

    # Nothing is left to wipe, so force_wipe only suppresses a prompt that would
    # never fire. Passing it keeps the run non-interactive either way.
    await _run(SEED_ORDER, force_wipe=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wipe Postgres, Elasticsearch and Redis, then reseed. DEV ONLY."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required. Confirms that destroying all local data is intended.",
    )
    args = parser.parse_args()

    if settings.env != "dev":
        print(f"Refusing to run with ENV={settings.env!r}. This script is dev-only.")
        raise SystemExit(1)

    if not args.yes:
        print(
            "This deletes every row in Postgres, the Elasticsearch GPS index, and\n"
            "the entire Redis database. Re-run with --yes if that is what you want."
        )
        raise SystemExit(1)

    asyncio.run(_reset())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - a dev script should say why plainly
        print(f"reset failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
