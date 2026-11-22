import re
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid4

from ecommerce_store_agent_orchestrator.application.agent_contracts import (
    AuthorSession,
    HandoffPin,
    ProposalProvenance,
    PublishedHandoff,
    QaProposal,
    SourceReference,
)
from ecommerce_store_agent_orchestrator.application.runs import RunRepository, WorkflowStepError
from ecommerce_store_agent_orchestrator.domain.evidence import HandoffSnapshot, ReviewSnapshot
from ecommerce_store_agent_orchestrator.domain.runs import RunInput, RunStatus, WorkClaim, WorkflowRun

_PRODUCTS_PATH = re.compile(r"/mobile-phones(?:/\{[A-Za-z0-9_]+\})?")


class AgentGateway(Protocol):
    def read_handoff(self, request: RunInput) -> PublishedHandoff: ...

    def create_author_session(self, handoff: HandoffSnapshot) -> AuthorSession: ...

    def put_proposal(self, session_id: UUID, proposal_id: UUID, request: RunInput) -> QaProposal: ...


def _validate_handoff(published: PublishedHandoff, pin: HandoffPin) -> HandoffSnapshot:
    if (published.session_id, published.id, published.draft_sha256) != (
        pin.ba_session_id,
        pin.ba_run_id,
        pin.draft_sha256,
    ) or published.approved_at > published.published_at:
        raise WorkflowStepError("handoff_version_mismatch", "The published BA run does not match the approved handoff.")

    operation = published.request
    criteria = published.draft.process.acceptance_criteria
    if (
        operation.service != "products"
        or operation.method != "GET"
        or _PRODUCTS_PATH.fullmatch(operation.path) is None
        or pin.criterion_index >= len(criteria)
    ):
        raise WorkflowStepError("unsupported_criterion", "Select the approved catalog-to-phone-details criterion.")

    draft = published.draft
    criterion = criteria[pin.criterion_index]
    if not all(text.strip() for text in (criterion.title, criterion.given, criterion.when, criterion.then)):
        raise WorkflowStepError("unsupported_criterion", "The BA criterion needs a title and Given, When, Then.")
    source_ids = {source.id for source in draft.sources}
    source_scope = (operation.service, draft.snapshot_id)
    if (
        len(source_ids) != len(draft.sources)
        or any((source.service, source.snapshot_id) != source_scope for source in draft.sources)
        or not any(source.path.startswith("sources/openapi.json#/") for source in draft.sources)
    ):
        raise WorkflowStepError("handoff_version_mismatch", "BA evidence does not match the pinned snapshot.")
    if draft.jira_context is not None:
        source_ids.add(f"jira:{draft.jira_context.key}")
    if not set(criterion.source_ids) <= source_ids:
        raise WorkflowStepError("handoff_version_mismatch", "The criterion references unavailable BA evidence.")

    return HandoffSnapshot(
        jira_issue_key=published.jira_issue.key,
        snapshot_id=draft.snapshot_id,
        evidence_exchange_id=draft.evidence_exchange_id,
        approved_at=published.approved_at,
        published_at=published.published_at,
        draft_sha256=published.draft_sha256,
        service=operation.service,
        method=operation.method,
        path=operation.path,
        criterion=criterion,
        sources=tuple(source.citation() for source in draft.sources),
    )


def _expected_provenance(request: RunInput, handoff: HandoffSnapshot) -> ProposalProvenance:
    return ProposalProvenance(
        ba_session_id=request.ba_session_id,
        ba_run_id=request.ba_run_id,
        snapshot_id=handoff.snapshot_id,
        draft_sha256=handoff.draft_sha256,
        evidence_exchange_id=handoff.evidence_exchange_id,
        jira_issue_key=handoff.jira_issue_key,
        criterion_index=request.criterion_index,
        criterion=handoff.criterion,
        sources=tuple(
            SourceReference(
                id=source.id,
                service=handoff.service,
                snapshot_id=handoff.snapshot_id,
                path=source.path,
                commit_sha=source.commit_sha,
            )
            for source in handoff.sources
        ),
    )


class HandoffAuthoring:
    def __init__(
        self,
        repository: RunRepository,
        agents: AgentGateway,
        pin: HandoffPin,
        pause_for_review: Callable[[WorkflowRun, ReviewSnapshot], None],
    ) -> None:
        self._repository = repository
        self._agents = agents
        self._pin = pin
        self._pause_for_review = pause_for_review

    def advance(self, claim: WorkClaim) -> None:
        if claim.run.status == RunStatus.VALIDATING_HANDOFF:
            self._pin_handoff(claim)
        else:
            self._author(claim)

    def _pin_handoff(self, claim: WorkClaim) -> None:
        request = claim.run.input
        pin = self._pin
        if request != RunInput(pin.ba_session_id, pin.ba_run_id, pin.criterion_index):
            raise WorkflowStepError("unsupported_criterion", "Select the configured catalog-to-phone-details handoff.")
        handoff = _validate_handoff(self._agents.read_handoff(request), pin)
        self._repository.start_authoring(claim, handoff, uuid4(), datetime.now(UTC))

    def _author(self, claim: WorkClaim) -> None:
        review = claim.run.review or self._create_review(claim)
        if review is None:
            return
        self._pause_for_review(claim.run, review)
        self._repository.mark_review_ready(claim, datetime.now(UTC))

    def _create_review(self, claim: WorkClaim) -> ReviewSnapshot | None:
        run = claim.run
        handoff = run.handoff
        if handoff is None or run.proposal_id is None:
            raise WorkflowStepError("invalid_qa_proposal", "The run is missing its pinned BA evidence or proposal ID.")

        session_id = run.qa_session_id
        if session_id is None:
            session = self._agents.create_author_session(handoff)
            if (session.snapshot_id, session.jira_issue_key) != (handoff.snapshot_id, handoff.jira_issue_key):
                raise WorkflowStepError("invalid_qa_proposal", "QA returned a session for different BA evidence.")
            session_id = session.id
            if not self._repository.record_qa_session(claim, session_id):
                return None

        proposal = self._agents.put_proposal(session_id, run.proposal_id, run.input)
        feature_hash = sha256(proposal.feature.encode("utf-8")).hexdigest()
        if proposal.provenance != _expected_provenance(run.input, handoff) or feature_hash != proposal.feature_sha256:
            raise WorkflowStepError(
                "invalid_qa_proposal", "The QA proposal does not match the pinned criterion or its hash."
            )

        review = ReviewSnapshot(
            qa_session_id=session_id,
            proposal_id=run.proposal_id,
            path=proposal.path,
            feature_sha256=proposal.feature_sha256,
            feature=proposal.feature,
        )
        return review if self._repository.record_review(claim, review) else None
