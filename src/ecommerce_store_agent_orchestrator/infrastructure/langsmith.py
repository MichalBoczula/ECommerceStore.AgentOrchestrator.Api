import logging
from uuid import uuid4

from httpx2 import Client, HTTPError

from ecommerce_store_agent_orchestrator.application.tracing import WorkTrace

logger = logging.getLogger(__name__)


class LangSmithTraceRecorder:
    def __init__(self, client: Client, project: str) -> None:
        self._client = client
        self._project = project

    def __call__(self, trace: WorkTrace) -> None:
        span_id = str(uuid4())
        metadata = {
            "correlation_id": trace.correlation_id,
            "stage": trace.stage.value,
            "attempt": trace.attempt,
            "outcome": trace.outcome,
        }
        body = {
            "id": span_id,
            "trace_id": span_id,
            "dotted_order": trace.started_at.strftime("%Y%m%dT%H%M%S%fZ") + span_id,
            "name": f"orchestrator.{trace.stage.value}",
            "run_type": "chain",
            "session_name": self._project,
            "start_time": trace.started_at.isoformat(),
            "end_time": trace.finished_at.isoformat(),
            "inputs": {},
            "outputs": {},
            "extra": {"metadata": metadata},
        }
        if trace.outcome != "processed":
            body["error"] = trace.outcome
        try:
            self._client.post("runs", json=body).raise_for_status()
        except HTTPError:
            logger.warning("LangSmith trace export failed.")
