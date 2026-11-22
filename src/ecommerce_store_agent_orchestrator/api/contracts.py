from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from ecommerce_store_agent_orchestrator.domain.evidence import HandoffSnapshot, ReviewSnapshot, RunFailure, Sha256
from ecommerce_store_agent_orchestrator.domain.review import CiEvidence, DecisionKind, Publication, ReviewDecision
from ecommerce_store_agent_orchestrator.domain.runs import RunInput, RunStatus


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, validate_by_name=True, from_attributes=True)


class RunInputPayload(ApiModel):
    ba_session_id: UUID
    ba_run_id: UUID
    criterion_index: int = Field(ge=0, strict=True)


class StartRunBody(RunInputPayload):
    model_config = ConfigDict(extra="forbid", validate_by_name=False)

    def to_domain(self) -> RunInput:
        return RunInput(self.ba_session_id, self.ba_run_id, self.criterion_index)


class RunAccepted(ApiModel):
    run_id: UUID


class DecisionInput(ApiModel):
    model_config = ConfigDict(extra="forbid", validate_by_name=False)

    decision: DecisionKind
    expected_feature_sha256: Sha256
    reason: Annotated[str, Field(max_length=2000, pattern=r"\S")] | None = None

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if self.reason is None and (self.decision == "reject" or "reason" in self.model_fields_set):
            raise ValueError("Supply a non-blank reason when rejecting or providing a reason.")
        return self


class DecisionAccepted(ApiModel):
    run_id: UUID
    status: Literal["publishing", "rejected"]


class RunSummary(ApiModel):
    run_id: UUID
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    jira_issue_key: str | None = None


class StageEventPayload(ApiModel):
    status: RunStatus
    at: datetime


class RunDetail(RunSummary):
    input: RunInputPayload
    history: list[StageEventPayload]
    handoff: HandoffSnapshot | None = None
    review: ReviewSnapshot | None = None
    decision: ReviewDecision | None = None
    publication: Publication | None = None
    ci: CiEvidence | None = None
    failure: RunFailure | None = None


class RunPagePayload(ApiModel):
    items: list[RunSummary]
    next_cursor: str | None = None
