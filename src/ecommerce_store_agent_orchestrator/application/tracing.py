from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ecommerce_store_agent_orchestrator.domain.runs import RunStatus

type WorkOutcome = Literal["processed", "retrying", "failed", "error"]


@dataclass(frozen=True, slots=True)
class WorkTrace:
    correlation_id: str
    stage: RunStatus
    attempt: int
    outcome: WorkOutcome
    started_at: datetime
    finished_at: datetime
