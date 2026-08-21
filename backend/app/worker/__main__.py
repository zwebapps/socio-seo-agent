"""`python -m backend.app.worker` — the scheduler as a process.

Its own entry point rather than a flag on the API, because the two have different
failure modes and different lifetimes: an API restart during a deploy must not abandon a
run mid-flight, and a scheduler that dies must not take the API with it.

**It loads `.env` the same way `asgi.py` does, and for the same reason.** That module is
documented as "the ONLY place that loads .env", and this is the second process the
project has — so the rule becomes "every process entry point loads it, and nothing
below one does". A worker that skipped it would connect to the default database rather
than the configured one, which is the kind of misconfiguration that looks like an empty
queue.
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

# Before importing anything that reads settings at module scope. `override=False` so a
# real environment variable beats the file, which is what a container expects.
load_dotenv(override=False)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from backend.app.db.adapters.run_store import PostgresRunStore
    from backend.app.services.run_executor import RunExecutor
    from backend.app.services.run_service import RunService
    from backend.app.worker.scheduler import run_forever

    # The SAME executor the API uses, constructed the same way. A second execution path
    # for a scheduled run would be a second place for the budget caps, the checkpointing
    # and the event stream to be got wrong — and the whole point of a scheduled run is
    # that it is an ordinary run nobody had to click.
    executor = RunExecutor(
        service_factory=lambda business_id: RunService(PostgresRunStore(business_id))
    )

    async def serve() -> None:
        try:
            await run_forever(submit=executor.submit)
        finally:
            # Let in-flight runs finish rather than dropping them on shutdown: a run
            # killed mid-node leaves a `running` row that the stranded sweep then has to
            # clean up two hours later.
            await executor.drain()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("scheduler: stopped")


if __name__ == "__main__":
    main()
