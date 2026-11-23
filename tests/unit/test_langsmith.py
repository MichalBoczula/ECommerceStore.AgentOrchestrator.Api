import json
from datetime import UTC, datetime
from logging import WARNING

from httpx2 import Client, MockTransport, Request, Response
from pytest import LogCaptureFixture

from ecommerce_store_agent_orchestrator.application.tracing import WorkTrace
from ecommerce_store_agent_orchestrator.domain.runs import RunStatus
from ecommerce_store_agent_orchestrator.infrastructure.langsmith import LangSmithTraceRecorder


def test_trace_outage_preserves_outcome_and_omits_server_response(caplog: LogCaptureFixture) -> None:
    def unavailable(request: Request) -> Response:
        body = json.loads(request.content)
        assert body["extra"]["metadata"]["outcome"] == "failed"
        assert body["inputs"] == body["outputs"] == {}
        return Response(503, text="private telemetry response")

    now = datetime.now(UTC)
    event = WorkTrace("a" * 64, RunStatus.VALIDATING_HANDOFF, 1, "failed", now, now)
    with Client(base_url="https://langsmith.example", transport=MockTransport(unavailable)) as client:
        recorder = LangSmithTraceRecorder(client, "synthetic")
        with caplog.at_level(WARNING):
            recorder(event)

    assert caplog.messages == ["LangSmith trace export failed."]
