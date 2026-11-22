from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ecommerce_store_agent_orchestrator.domain.evidence import HandoffSnapshot, ReviewSnapshot, RunFailure
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
    failure: RunFailure | None = None


class RunPagePayload(ApiModel):
    items: list[RunSummary]
    next_cursor: str | None = None
