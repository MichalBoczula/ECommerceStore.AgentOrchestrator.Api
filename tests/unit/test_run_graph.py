import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ecommerce_store_agent_orchestrator.application.runs import WorkflowStepError
from ecommerce_store_agent_orchestrator.infrastructure.run_graph import build_preparation_graph, thread_config

FEATURE_HASH = "a" * 64


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [("approve", "publishing"), ("reject", "rejected"), ("approve", None)],
    ids=["approve-exact-version", "reject-exact-version", "refuse-stale-version"],
)
def test_review_transition_requires_the_pending_feature_hash(decision: str, expected_status: str | None) -> None:
    graph = build_preparation_graph(InMemorySaver())
    config = thread_config("review-transition-unit")
    graph.invoke(
        {
            "run_id": "review-transition-unit",
            "ba_session_id": "ba-session",
            "ba_run_id": "ba-run",
            "criterion_index": 0,
            "graph_version": 1,
            "feature_sha256": FEATURE_HASH,
        },
        config,
        durability="sync",
    )
    pending = graph.get_state(config)
    assert pending.values["status"] == "validating_handoff"
    assert [item.value for item in pending.interrupts] == [{"featureSha256": FEATURE_HASH}]
    reviewed_hash = FEATURE_HASH if expected_status is not None else "b" * 64
    resume = Command(resume={"decision": decision, "featureSha256": reviewed_hash})

    if expected_status is None:
        with pytest.raises(WorkflowStepError, match="does not match this review checkpoint"):
            graph.invoke(resume, config, durability="sync")
        assert "review_decision" not in graph.get_state(config).values
    else:
        output = graph.invoke(resume, config, durability="sync")
        assert output["status"] == expected_status
        assert output["review_decision"] == decision
        assert not graph.get_state(config).interrupts
