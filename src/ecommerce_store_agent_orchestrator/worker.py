import argparse
import logging
import signal
from datetime import timedelta
from threading import Event

from langgraph.checkpoint.mongodb import MongoDBSaver
from pymongo.errors import PyMongoError

from ecommerce_store_agent_orchestrator.application.worker import RunWorker
from ecommerce_store_agent_orchestrator.infrastructure.mongo import MongoRunRepository, create_client
from ecommerce_store_agent_orchestrator.infrastructure.run_graph import RunPreparation
from ecommerce_store_agent_orchestrator.infrastructure.settings import WorkerSettings

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process persisted Agent Orchestrator work.")
    parser.add_argument("--once", action="store_true", help="Check for and process one ready run, then exit.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = WorkerSettings()

    with create_client(settings) as client:
        repository = MongoRunRepository(client[settings.mongodb_database])
        repository.ensure_indexes()
        checkpointer = MongoDBSaver(client, db_name=settings.mongodb_database)
        preparation = RunPreparation(checkpointer)
        worker = RunWorker(repository, preparation.prepare, timedelta(seconds=settings.worker_lease_seconds))

        if args.once:
            worker.process_one()
        else:
            _run_worker(worker, settings.worker_poll_seconds)


def _run_worker(worker: RunWorker, poll_seconds: float) -> None:
    stopped = Event()

    def request_stop(*_args: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    while not stopped.is_set():
        try:
            if worker.process_one():
                continue
        except PyMongoError:
            logger.warning("Run storage unavailable. The worker will retry.")
        stopped.wait(poll_seconds)


if __name__ == "__main__":
    main()
