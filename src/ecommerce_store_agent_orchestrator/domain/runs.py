from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


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


@dataclass(frozen=True, slots=True)
class WorkClaim:
    run: WorkflowRun
    token: UUID


@dataclass(frozen=True, slots=True)
class RunPosition:
    created_at: datetime
    run_id: UUID
