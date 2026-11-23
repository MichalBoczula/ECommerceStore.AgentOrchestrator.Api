from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from ecommerce_store_agent_orchestrator.domain.evidence import EvidenceModel, NonBlank, Sha256

type DecisionKind = Literal["approve", "reject"]
type PublicationKind = Literal["pull_request", "already_current"]
type GitSha = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
type PullNumber = Annotated[int, Field(ge=1, strict=True)]


class Reviewer(EvidenceModel):
    issuer: str
    subject: str


class ReviewDecision(EvidenceModel):
    decision: DecisionKind
    feature_sha256: Sha256
    reviewer: Reviewer
    decided_at: AwareDatetime
    reason: str | None = None


class PublicationRequest(EvidenceModel):
    qa_session_id: UUID
    proposal_id: UUID
    feature_sha256: Sha256


class PublicationReceipt(EvidenceModel):
    status: PublicationKind
    repository: NonBlank
    number: PullNumber | None = None
    branch: NonBlank | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if self.status == "pull_request" and (self.number is None or self.branch is None):
            raise ValueError("QA must provide the pull number and branch.")
        return self


class Publication(EvidenceModel):
    outcome: PublicationKind
    repository: str
    url: str
    commit_sha: GitSha
    pull_request_number: PullNumber | None = None
    branch: str | None = None


type EvidenceId = Annotated[int, Field(ge=1, strict=True)]
type PlaywrightResult = Literal["pending", "passed", "failed", "not_run", "evidence_missing", "not_applicable"]
type GherkinCheckResult = Literal["pending", "passed", "failed", "not_run"]


class AllureArtifact(EvidenceModel):
    id: EvidenceId
    name: str
    url: str


class CiEvidence(EvidenceModel):
    status: PlaywrightResult
    gherkin_check_status: GherkinCheckResult
    reason_code: str | None = None
    workflow_run_id: EvidenceId | None = None
    workflow_run_url: str | None = None
    run_attempt: EvidenceId | None = None
    storefront_job_id: EvidenceId | None = None
    observed_head_sha: GitSha | None = None
    tested_commit_sha: GitSha | None = None
    current_head_sha: GitSha | None = None
    allure_artifact: AllureArtifact | None = None
