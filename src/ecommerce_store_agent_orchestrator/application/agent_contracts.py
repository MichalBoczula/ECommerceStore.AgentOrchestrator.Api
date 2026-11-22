from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from ecommerce_store_agent_orchestrator.domain.evidence import Citation, Criterion, EvidenceModel, NonBlank, Sha256


class HandoffPin(EvidenceModel):
    ba_session_id: UUID
    ba_run_id: UUID
    draft_sha256: Sha256
    criterion_index: int = Field(ge=0, strict=True)


class Operation(EvidenceModel):
    service: str
    method: str
    path: str


class JiraReference(EvidenceModel):
    key: NonBlank


class SourceReference(EvidenceModel):
    id: str
    service: str
    snapshot_id: str
    path: str
    commit_sha: str


class BaSource(SourceReference):
    title: str
    url: str | None = None

    def citation(self) -> Citation:
        url = self.url if self.url and self.url.startswith(("https://", "http://")) else None
        return Citation(id=self.id, title=self.title, path=self.path, commit_sha=self.commit_sha, url=url)


class Process(EvidenceModel):
    acceptance_criteria: tuple[Criterion, ...]


class Draft(EvidenceModel):
    snapshot_id: NonBlank
    evidence_exchange_id: UUID
    process: Process
    sources: tuple[BaSource, ...]
    jira_context: JiraReference | None = None


class PublishedHandoff(EvidenceModel):
    id: UUID
    session_id: UUID
    status: Literal["published"]
    approved_at: AwareDatetime
    published_at: AwareDatetime
    draft_sha256: Sha256
    request: Operation
    draft: Draft
    jira_issue: JiraReference


class AuthorSession(EvidenceModel):
    id: UUID
    operation: Literal["author"]
    jira_issue_key: NonBlank
    snapshot_id: NonBlank


class ProposalProvenance(EvidenceModel):
    ba_session_id: UUID
    ba_run_id: UUID
    snapshot_id: NonBlank
    draft_sha256: Sha256
    evidence_exchange_id: UUID
    jira_issue_key: NonBlank
    criterion_index: int = Field(ge=0, strict=True)
    criterion: Criterion
    sources: tuple[SourceReference, ...]


class QaProposal(EvidenceModel):
    provenance: ProposalProvenance
    path: Literal["features/catalog/browse-phone-details.feature"]
    feature: NonBlank
    feature_sha256: Sha256
