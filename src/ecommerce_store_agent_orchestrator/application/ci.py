from datetime import UTC, datetime, timedelta
from typing import Protocol

from ecommerce_store_agent_orchestrator.application.runs import RunRepository, WorkflowStepError
from ecommerce_store_agent_orchestrator.domain.review import CiEvidence, Publication
from ecommerce_store_agent_orchestrator.domain.runs import WorkClaim

_POLL_INTERVAL = timedelta(seconds=30)


class CiReader(Protocol):
    def observe(self, publication: Publication, feature_hash: str) -> CiEvidence: ...


class CiObservation:
    def __init__(self, repository: RunRepository, reader: CiReader) -> None:
        self._repository = repository
        self._reader = reader

    def advance(self, claim: WorkClaim) -> None:
        publication = claim.run.publication
        approval = claim.run.publication_request
        if publication is None or publication.outcome != "pull_request" or approval is None:
            raise WorkflowStepError("publication_conflict", "The run has no verified publication to observe.")
        evidence = self._reader.observe(publication, approval.feature_sha256)
        now = datetime.now(UTC)
        poll_at = now + _POLL_INTERVAL if evidence.status == "pending" else None
        self._repository.save_ci_evidence(claim, evidence, now, poll_at)
