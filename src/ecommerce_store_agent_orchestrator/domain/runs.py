from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from uuid import UUID

from ecommerce_store_agent_orchestrator.domain.evidence import HandoffSnapshot, ReviewSnapshot, RunFailure
from ecommerce_store_agent_orchestrator.domain.review import (
    CiEvidence,
    Publication,
    PublicationReceipt,
    PublicationRequest,
    ReviewDecision,
)


class RunStatus(StrEnum):
    QUEUED = "queued"
    VALIDATING_HANDOFF = "validating_handoff"
    AUTHORING_PROPOSAL = "authoring_proposal"
    AWAITING_REVIEW = "awaiting_review"
    REJECTED = "rejected"
    PUBLISHING = "publishing"
    AWAITING_CI = "awaiting_ci"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunOwner:
    issuer: str
    subject: str


@dataclass(frozen=True, slots=True)
class RunInput:
    ba_session_id: UUID
    ba_run_id: UUID
    criterion_index: int


@dataclass(frozen=True, slots=True)
class StageEvent:
    status: RunStatus
    at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    run_id: UUID
    owner: RunOwner
    input: RunInput
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    history: tuple[StageEvent, ...]
    graph_version: int = 1
    handoff: HandoffSnapshot | None = None
    proposal_id: UUID | None = None
    qa_session_id: UUID | None = None
    review: ReviewSnapshot | None = None
    failure: RunFailure | None = None
    decision: ReviewDecision | None = None
    publication_request: PublicationRequest | None = None
    publication_receipt: PublicationReceipt | None = None
    publication: Publication | None = None
    ci: CiEvidence | None = None

    @property
    def jira_issue_key(self) -> str | None:
        return self.handoff.jira_issue_key if self.handoff is not None else None

    @property
    def trace_correlation_id(self) -> str:
        return sha256(b"ecommerce-store-agent-orchestrator:" + self.run_id.bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkClaim:
    run: WorkflowRun
    token: UUID
    attempts: int = 1


@dataclass(frozen=True, slots=True)
class RunPosition:
    created_at: datetime
    run_id: UUID
