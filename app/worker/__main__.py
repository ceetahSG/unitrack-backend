"""UniTrack worker process (spec §4.1 worker layer).

Jobs:
  1. GPS ES indexer — read gps_ingest stream, index to Elasticsearch   [wired]
                      (ES is the sole GPS store; Postgres holds no GPS.)
  2. Payment reconciler — settle orders no report ever arrived for     [wired]
  3. ETA engine — arrival times per live trip, cached in Redis         [wired]
                  (free path: observed speed + schedule delay, no Mapbox)
  4. Fraud sweep — suspend tickets whose code was reused offline      [wired]
  5. Report aggregation                                              [later]

Jobs run concurrently in one process and must not take each other down, so each
owns its own error handling. `asyncio.gather` would cancel the siblings of any
task that raised, which would mean a payment gateway outage silently stopping
GPS indexing.
"""

import asyncio
import logging

from app.worker.eta_engine import run as run_eta_engine
from app.worker.fraud_sweep import run as run_fraud_sweep
from app.worker.gps_es_indexer import run as run_gps_es_indexer
from app.worker.payment_reconciler import run as run_payment_reconciler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("unitrack.worker")


async def main() -> None:
    logger.info("UniTrack worker starting.")
    await asyncio.gather(
        run_gps_es_indexer(),
        run_payment_reconciler(),
        run_eta_engine(),
        run_fraud_sweep(),
    )


if __name__ == "__main__":
    asyncio.run(main())
