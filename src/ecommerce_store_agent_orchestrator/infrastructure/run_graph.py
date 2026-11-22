from typing import NotRequired, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ecommerce_store_agent_orchestrator.application.runs import WorkflowStepError
from ecommerce_store_agent_orchestrator.domain.evidence import ReviewSnapshot
from ecommerce_store_agent_orchestrator.domain.runs import RunStatus, WorkflowRun


class PreparationState(TypedDict):
    run_id: str
    ba_session_id: str
    ba_run_id: str
    criterion_index: int
    graph_version: int
    status: NotRequired[str]
    qa_session_id: NotRequired[str]
    proposal_id: NotRequired[str]
    feature_sha256: NotRequired[str]
    review_decision: NotRequired[str]


def preparation_input(run: WorkflowRun) -> PreparationState:
    return {
        "run_id": str(run.run_id),
        "ba_session_id": str(run.input.ba_session_id),
        "ba_run_id": str(run.input.ba_run_id),
        "criterion_index": run.input.criterion_index,
        "graph_version": run.graph_version,
    }


def thread_config(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}}


def _stage_handoff(_: PreparationState) -> dict[str, str]:
    return {"status": RunStatus.VALIDATING_HANDOFF.value}


def _review(state: PreparationState) -> dict[str, str]:
    resume = interrupt({"featureSha256": state["feature_sha256"]})
    if (
        not isinstance(resume, dict)
        or resume.get("featureSha256") != state["feature_sha256"]
        or resume.get("decision") not in ("approve", "reject")
    ):
        raise WorkflowStepError("publication_conflict", "The recorded decision does not match this review checkpoint.")
    decision = resume["decision"]
    status = RunStatus.PUBLISHING if decision == "approve" else RunStatus.REJECTED
    return {"review_decision": decision, "status": status.value}


def _after_handoff(state: PreparationState) -> str:
    return "review" if "feature_sha256" in state else END


def build_preparation_graph(checkpointer: BaseCheckpointSaver):
    builder = StateGraph(PreparationState)
    builder.add_node("stage_handoff", _stage_handoff)
    builder.add_node("review", _review)
    builder.add_edge(START, "stage_handoff")
    builder.add_conditional_edges("stage_handoff", _after_handoff, ["review", END])
    builder.add_edge("review", END)
    return builder.compile(checkpointer=checkpointer)


class RunPreparation:
    def __init__(self, checkpointer: BaseCheckpointSaver) -> None:
        self._graph = build_preparation_graph(checkpointer)

    def prepare(self, run: WorkflowRun) -> None:
        config = thread_config(str(run.run_id))
        initial = preparation_input(run)
        checkpoint = self._graph.get_state(config)
        if checkpoint.next:
            output = self._graph.invoke(None, config, durability="sync")
        elif not checkpoint.values:
            output = self._graph.invoke(initial, config, durability="sync")
        else:
            output = checkpoint.values

        output = {"graph_version": 1, **output}
        if output.get("status") != RunStatus.VALIDATING_HANDOFF.value or any(
            output.get(key) != value for key, value in initial.items()
        ):
            raise RuntimeError("The checkpoint does not match the registered run")

    def pause_for_review(self, run: WorkflowRun, review: ReviewSnapshot) -> None:
        config = thread_config(str(run.run_id))
        checkpoint = self._graph.get_state(config)
        if checkpoint.values.get("run_id") != str(run.run_id):
            raise RuntimeError("Run preparation must be checkpointed before review")

        expected = {
            "qa_session_id": str(review.qa_session_id),
            "proposal_id": str(review.proposal_id),
            "feature_sha256": review.feature_sha256,
        }
        if "feature_sha256" not in checkpoint.values:
            self._graph.update_state(config, expected, as_node="stage_handoff")
            checkpoint = self._graph.get_state(config)
        if any(checkpoint.values.get(key) != value for key, value in expected.items()):
            raise RuntimeError("The checkpoint refers to a different QA proposal")

        if not checkpoint.interrupts and checkpoint.next == ("review",):
            self._graph.invoke(None, config, durability="sync")
            checkpoint = self._graph.get_state(config)
        if [item.value for item in checkpoint.interrupts] != [{"featureSha256": review.feature_sha256}]:
            raise RuntimeError("The persisted review interrupt does not match the QA proposal")

    def resume_review(self, run: WorkflowRun) -> None:
        decision = run.decision
        review = run.review
        if decision is None or review is None:
            raise WorkflowStepError("publication_conflict", "The run is missing the saved review or decision.")

        config = thread_config(str(run.run_id))
        checkpoint = self._graph.get_state(config)
        expected = {
            "run_id": str(run.run_id),
            "qa_session_id": str(review.qa_session_id),
            "proposal_id": str(review.proposal_id),
            "feature_sha256": decision.feature_sha256,
        }
        if any(checkpoint.values.get(key) != value for key, value in expected.items()):
            raise WorkflowStepError("publication_conflict", "The decision refers to a different QA proposal.")

        if "review_decision" not in checkpoint.values:
            pending = [item.value for item in checkpoint.interrupts]
            if pending != [{"featureSha256": decision.feature_sha256}]:
                raise WorkflowStepError("publication_conflict", "The pending review checkpoint is unavailable.")
            self._graph.invoke(
                Command(resume={"decision": decision.decision, "featureSha256": decision.feature_sha256}),
                config,
                durability="sync",
            )
            checkpoint = self._graph.get_state(config)

        if checkpoint.values.get("review_decision") != decision.decision or checkpoint.interrupts:
            raise WorkflowStepError("publication_conflict", "The review checkpoint contains a different decision.")
