import json
from base64 import b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict

from ecommerce_store_agent_orchestrator.domain.evidence import FailureCode, HandoffSnapshot, ReviewSnapshot, RunFailure
from ecommerce_store_agent_orchestrator.domain.review import (
    CiEvidence,
    DecisionKind,
    Publication,
    PublicationReceipt,
    ReviewDecision,
    Reviewer,
)
from ecommerce_store_agent_orchestrator.domain.runs import (
    RunInput,
    RunOwner,
    RunPosition,
    RunStatus,
    StageEvent,
    WorkClaim,
    WorkflowRun,
)


class IdempotencyConflictError(Exception):
    pass


class InvalidCursorError(Exception):
    pass


class RunNotFoundError(Exception):
    pass


class StaleFeatureHashError(Exception):
    pass


class ReviewClosedError(Exception):
    pass


class WorkflowStepError(Exception):
    def __init__(self, code: FailureCode, detail: str) -> None:
        super().__init__(detail)
        self.failure = RunFailure(code=code, detail=detail)


class RunRepository(Protocol):
    def insert_or_get(self, run: WorkflowRun, idempotency_key: str) -> WorkflowRun: ...

    def get(self, owner: RunOwner, run_id: UUID) -> WorkflowRun | None: ...

    def page(
        self, owner: RunOwner, status: RunStatus | None, after: RunPosition | None, limit: int
    ) -> list[WorkflowRun]: ...

    def claim_ready(
        self,
        now: datetime,
        lease: timedelta,
        *,
        with_authoring: bool = False,
        with_publication: bool = False,
        with_ci: bool = False,
    ) -> WorkClaim | None: ...

    def mark_handoff_ready(self, claim: WorkClaim, at: datetime) -> bool: ...

    def start_authoring(self, claim: WorkClaim, handoff: HandoffSnapshot, proposal_id: UUID, at: datetime) -> bool: ...

    def record_qa_session(self, claim: WorkClaim, session_id: UUID) -> bool: ...

    def record_review(self, claim: WorkClaim, review: ReviewSnapshot) -> bool: ...

    def mark_review_ready(self, claim: WorkClaim, at: datetime) -> bool: ...

    def record_decision(self, run: WorkflowRun, decision: ReviewDecision) -> RunStatus | None: ...

    def finish_rejection(self, claim: WorkClaim) -> bool: ...

    def record_publication_receipt(self, claim: WorkClaim, receipt: PublicationReceipt) -> bool: ...

    def finish_publication(self, claim: WorkClaim, publication: Publication, ci: CiEvidence, at: datetime) -> bool: ...

    def save_ci_evidence(
        self, claim: WorkClaim, ci: CiEvidence, at: datetime, next_poll_at: datetime | None
    ) -> bool: ...

    def fail_work(self, claim: WorkClaim, failure: RunFailure, at: datetime) -> bool: ...


@dataclass(frozen=True, slots=True)
class RunPage:
    items: list[WorkflowRun]
    next_cursor: str | None


class _Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_hash: str
    status: RunStatus | None
    created_at: AwareDatetime
    run_id: UUID


def _owner_hash(owner: RunOwner) -> str:
    return sha256(json.dumps([owner.issuer, owner.subject]).encode()).hexdigest()


def _decode_cursor(cursor: str, owner_hash: str, status: RunStatus | None) -> RunPosition:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        parsed = _Cursor.model_validate_json(b64decode(padded, altchars=b"-_", validate=True))
        created_at = parsed.created_at.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise InvalidCursorError from exc
    if parsed.owner_hash != owner_hash or parsed.status != status:
        raise InvalidCursorError
    return RunPosition(created_at=created_at, run_id=parsed.run_id)


def _check_review(run: WorkflowRun, expected_hash: str) -> None:
    if run.status != RunStatus.AWAITING_REVIEW or run.review is None:
        raise ReviewClosedError
    if run.review.feature_sha256 != expected_hash:
        raise StaleFeatureHashError


class RunService:
    def __init__(self, repository: RunRepository) -> None:
        self._repository = repository

    def start(self, owner: RunOwner, idempotency_key: str, request: RunInput) -> UUID:
        now = datetime.now(UTC)
        new_run = WorkflowRun(
            run_id=uuid4(),
            owner=owner,
            input=request,
            status=RunStatus.QUEUED,
            created_at=now,
            updated_at=now,
            history=(StageEvent(RunStatus.QUEUED, now),),
        )
        run = self._repository.insert_or_get(new_run, idempotency_key)
        if run.input != request:
            raise IdempotencyConflictError
        return run.run_id

    def get(self, owner: RunOwner, run_id: UUID) -> WorkflowRun:
        run = self._repository.get(owner, run_id)
        if run is None:
            raise RunNotFoundError
        return run

    def decide(
        self, owner: RunOwner, run_id: UUID, decision: DecisionKind, expected_hash: str, reason: str | None
    ) -> RunStatus:
        run = self.get(owner, run_id)
        _check_review(run, expected_hash)
        record = ReviewDecision(
            decision=decision,
            feature_sha256=expected_hash,
            reviewer=Reviewer(issuer=owner.issuer, subject=owner.subject),
            decided_at=datetime.now(UTC),
            reason=reason,
        )
        status = self._repository.record_decision(run, record)
        if status is None:
            _check_review(self.get(owner, run_id), expected_hash)
            raise ReviewClosedError
        return status

    def list(self, owner: RunOwner, status: RunStatus | None, limit: int, cursor: str | None) -> RunPage:
        owner_hash = _owner_hash(owner)
        after = _decode_cursor(cursor, owner_hash, status) if cursor is not None else None
        runs = self._repository.page(owner, status, after, limit + 1)
        next_cursor = None
        if len(runs) > limit:
            last = runs[limit - 1]
            payload = _Cursor(
                owner_hash=owner_hash,
                status=status,
                created_at=last.created_at,
                run_id=last.run_id,
            )
            next_cursor = urlsafe_b64encode(payload.model_dump_json().encode()).decode().rstrip("=")
        return RunPage(items=runs[:limit], next_cursor=next_cursor)
