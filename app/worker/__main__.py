"""UniTrack worker process (spec §4.1 worker layer).

Jobs:
  1. GPS ES indexer — read gps_ingest stream, index to Elasticsearch   [wired]
                      (ES is the sole GPS store; Postgres holds no GPS.)
  2. Payment reconciler — settle orders no report ever arrived for     [wired]
  3. ETA engine    — Mapbox Directions per live trip every 2-3 min   [later]
  4. Fraud sweep + auto-alerts + report aggregation                  [later]

Jobs run concurrently in one process and must not take each other down, so each
owns its own error handling. `asyncio.gather` would cancel the siblings of any
task that raised, which would mean a payment gateway outage silently stopping
GPS indexing.
"""

import asyncio
import logging

from app.worker.gps_es_indexer import run as run_gps_es_indexer
from app.worker.payment_reconciler import run as run_payment_reconciler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("unitrack.worker")


async def main() -> None:
    logger.info("UniTrack worker starting.")
    await asyncio.gather(
        run_gps_es_indexer(),
        run_payment_reconciler(),
    )


if __name__ == "__main__":
    asyncio.run(main())
