from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

type Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
type NonBlank = Annotated[str, Field(pattern=r"\S")]
type FailureCode = Literal[
    "handoff_not_found",
    "handoff_not_published",
    "handoff_version_mismatch",
    "unsupported_criterion",
    "invalid_qa_proposal",
    "publication_conflict",
    "publication_revision_mismatch",
    "integration_unavailable",
]


class EvidenceModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, frozen=True)


class Criterion(EvidenceModel):
    title: str
    given: str
    when: str
    then: str
    source_ids: tuple[str, ...] = Field(min_length=1)


class Citation(EvidenceModel):
    id: str
    title: str
    path: str
    commit_sha: str
    url: str | None = None


class HandoffSnapshot(EvidenceModel):
    jira_issue_key: str
    snapshot_id: NonBlank
    evidence_exchange_id: UUID
    approved_at: AwareDatetime
    published_at: AwareDatetime
    draft_sha256: Sha256
    service: str
    method: str
    path: str
    criterion: Criterion
    sources: tuple[Citation, ...]


class ReviewSnapshot(EvidenceModel):
    qa_session_id: UUID
    proposal_id: UUID
    path: str
    feature_sha256: Sha256
    feature: NonBlank


class RunFailure(EvidenceModel):
    code: FailureCode
    detail: str
