import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ecommerce_store_agent_orchestrator.application.authoring import HandoffAuthoring
from ecommerce_store_agent_orchestrator.application.ci import CiObservation
from ecommerce_store_agent_orchestrator.application.review import ReviewExecution
from ecommerce_store_agent_orchestrator.application.runs import RunRepository, WorkflowStepError
from ecommerce_store_agent_orchestrator.domain.runs import RunStatus, WorkClaim, WorkflowRun

logger = logging.getLogger(__name__)
_MAX_INTEGRATION_ATTEMPTS = 3


class RunWorker:
    def __init__(
        self,
        repository: RunRepository,
        prepare_run: Callable[[WorkflowRun], None],
        lease: timedelta,
        review: ReviewExecution,
        ci: CiObservation,
        authoring: HandoffAuthoring | None = None,
    ) -> None:
        self._repository = repository
        self._prepare_run = prepare_run
        self._lease = lease
        self._review = review
        self._ci = ci
        self._authoring = authoring

    def process_one(self) -> bool:
        claim = self._repository.claim_ready(
            datetime.now(UTC),
            self._lease,
            with_authoring=self._authoring is not None,
            with_publication=self._review.can_publish,
            with_ci=True,
        )
        if claim is None:
            return False

        try:
            self._advance(claim)
        except WorkflowStepError as error:
            if error.failure.code != "integration_unavailable" or claim.attempts >= _MAX_INTEGRATION_ATTEMPTS:
                self._repository.fail_work(claim, error.failure, datetime.now(UTC))
        return True

    def _advance(self, claim: WorkClaim) -> None:
        if claim.run.status == RunStatus.QUEUED:
            self._prepare_run(claim.run)
            if not self._repository.mark_handoff_ready(claim, datetime.now(UTC)):
                logger.info("Work claim changed for run %s", claim.run.run_id)
        elif claim.run.status in (RunStatus.PUBLISHING, RunStatus.REJECTED):
            self._review.advance(claim)
        elif claim.run.status == RunStatus.AWAITING_CI:
            self._ci.advance(claim)
        elif self._authoring is not None:
            self._authoring.advance(claim)
