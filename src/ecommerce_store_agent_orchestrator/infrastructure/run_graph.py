from typing import NotRequired, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from ecommerce_store_agent_orchestrator.domain.runs import RunStatus, WorkflowRun


class PreparationState(TypedDict):
    run_id: str
    ba_session_id: str
    ba_run_id: str
    criterion_index: int
    status: NotRequired[str]


def preparation_input(run: WorkflowRun) -> PreparationState:
    return {
        "run_id": str(run.run_id),
        "ba_session_id": str(run.input.ba_session_id),
        "ba_run_id": str(run.input.ba_run_id),
        "criterion_index": run.input.criterion_index,
    }


def thread_config(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}}


def _stage_handoff(_: PreparationState) -> dict[str, str]:
    return {"status": RunStatus.VALIDATING_HANDOFF.value}


def build_preparation_graph(checkpointer: BaseCheckpointSaver):
    builder = StateGraph(PreparationState)
    builder.add_node("stage_handoff", _stage_handoff)
    builder.add_edge(START, "stage_handoff")
    builder.add_edge("stage_handoff", END)
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

        if output.get("status") != RunStatus.VALIDATING_HANDOFF.value or any(
            output.get(key) != value for key, value in initial.items()
        ):
            raise RuntimeError("The checkpoint does not match the registered run")
