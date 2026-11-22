import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ecommerce_store_agent_orchestrator.application.authoring import HandoffAuthoring
from ecommerce_store_agent_orchestrator.application.runs import RunRepository
from ecommerce_store_agent_orchestrator.domain.runs import RunStatus, WorkflowRun

logger = logging.getLogger(__name__)


class RunWorker:
    def __init__(
        self,
        repository: RunRepository,
        prepare_run: Callable[[WorkflowRun], None],
        lease: timedelta,
        authoring: HandoffAuthoring | None = None,
    ) -> None:
        self._repository = repository
        self._prepare_run = prepare_run
        self._lease = lease
        self._authoring = authoring

    def process_one(self) -> bool:
        claim = self._repository.claim_ready(datetime.now(UTC), self._lease, with_authoring=self._authoring is not None)
        if claim is None:
            return False

        if claim.run.status == RunStatus.QUEUED:
            self._prepare_run(claim.run)
            if not self._repository.mark_handoff_ready(claim, datetime.now(UTC)):
                logger.info("Work claim changed for run %s", claim.run.run_id)
        elif self._authoring is not None:
            self._authoring.advance(claim)
        return True
