from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from ecommerce_store_agent_orchestrator.application.runs import RunRepository, WorkflowStepError
from ecommerce_store_agent_orchestrator.domain.review import (
    CiEvidence,
    Publication,
    PublicationReceipt,
    PublicationRequest,
)
from ecommerce_store_agent_orchestrator.domain.runs import RunStatus, WorkClaim, WorkflowRun


class ProposalPublisher(Protocol):
    def publish_proposal(self, request: PublicationRequest) -> PublicationReceipt: ...


class PublicationReader(Protocol):
    def verify(self, receipt: PublicationReceipt, feature_path: str, feature_hash: str) -> Publication: ...


class ReviewExecution:
    def __init__(
        self,
        repository: RunRepository,
        resume_review: Callable[[WorkflowRun], None],
        publisher: ProposalPublisher | None,
        reader: PublicationReader | None,
    ) -> None:
        self._repository = repository
        self._resume_review = resume_review
        self._publisher = publisher
        self._reader = reader

    @property
    def can_publish(self) -> bool:
        return self._publisher is not None and self._reader is not None

    def advance(self, claim: WorkClaim) -> None:
        run = claim.run
        self._resume_review(run)
        if run.status == RunStatus.REJECTED:
            self._repository.finish_rejection(claim)
            return

        review, decision, request = run.review, run.decision, run.publication_request
        if (
            review is None
            or decision is None
            or decision.decision != "approve"
            or request
            != PublicationRequest(
                qa_session_id=review.qa_session_id,
                proposal_id=review.proposal_id,
                feature_sha256=decision.feature_sha256,
            )
        ):
            raise WorkflowStepError("publication_conflict", "QA publication inputs do not match the saved approval.")

        publisher, reader = self._publisher, self._reader
        assert publisher is not None and reader is not None
        receipt = run.publication_receipt
        if receipt is None:
            receipt = publisher.publish_proposal(request)
            if not self._repository.record_publication_receipt(claim, receipt):
                return

        result = reader.verify(receipt, review.path, request.feature_sha256)
        ci = (
            CiEvidence(status="pending", gherkin_check_status="pending")
            if result.outcome == "pull_request"
            else CiEvidence(status="not_applicable", gherkin_check_status="not_run", reason_code="already_on_main")
        )
        self._repository.finish_publication(claim, result, ci, datetime.now(UTC))
