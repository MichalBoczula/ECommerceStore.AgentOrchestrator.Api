import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ecommerce_store_agent_orchestrator.application.runs import RunRepository
from ecommerce_store_agent_orchestrator.domain.runs import WorkflowRun

logger = logging.getLogger(__name__)


class RunWorker:
    def __init__(self, repository: RunRepository, prepare_run: Callable[[WorkflowRun], None], lease: timedelta) -> None:
        self._repository = repository
        self._prepare_run = prepare_run
        self._lease = lease

    def process_one(self) -> bool:
        claim = self._repository.claim_ready(datetime.now(UTC), self._lease)
        if claim is None:
            return False

        self._prepare_run(claim.run)
        if not self._repository.mark_handoff_ready(claim, datetime.now(UTC)):
            logger.info("Work claim changed for run %s", claim.run.run_id)
        return True
