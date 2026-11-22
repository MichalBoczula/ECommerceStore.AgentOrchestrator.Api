# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = [
#     "langgraph==1.2.24",
#     "langgraph-checkpoint-mongodb==0.5.1",
# ]
# ///

import argparse
import os
from hashlib import sha256
from typing import Literal, NotRequired, TypedDict
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

FEATURE_HASH = sha256(b"Feature: Review the synthetic storefront scenario\n").hexdigest()


class ReviewState(TypedDict):
    feature_sha256: str
    decision: NotRequired[Literal["approve"]]


def review(state: ReviewState) -> dict[str, str]:
    feature_hash = state["feature_sha256"]
    response = interrupt({"featureSha256": feature_hash})
    if response != {"decision": "approve", "featureSha256": feature_hash}:
        raise ValueError("The resumed decision must approve the pending feature hash")
    return {"decision": "approve"}


def exercise_phase(
    checkpointer: BaseCheckpointSaver, phase: Literal["pause", "resume"], run_id: UUID
) -> None:
    builder = StateGraph(ReviewState)
    builder.add_node("review", review)
    builder.add_edge(START, "review")
    builder.add_edge("review", END)
    graph = builder.compile(checkpointer=checkpointer)
    config: RunnableConfig = {"configurable": {"thread_id": str(run_id)}}

    if phase == "pause":
        graph.invoke({"feature_sha256": FEATURE_HASH}, config, durability="sync")

    pending = graph.get_state(config)
    assert pending.values["feature_sha256"] == FEATURE_HASH
    assert pending.next == ("review",)
    assert [item.value for item in pending.interrupts] == [{"featureSha256": FEATURE_HASH}]

    if phase == "resume":
        decision = {"decision": "approve", "featureSha256": FEATURE_HASH}
        graph.invoke(Command(resume=decision), config, durability="sync")
        completed = graph.get_state(config)
        assert completed.values["feature_sha256"] == FEATURE_HASH
        assert completed.values["decision"] == "approve"
        assert not completed.next
        assert not completed.interrupts

    print(f"{phase}: verified thread {run_id}")


def main() -> None:
    from langgraph.checkpoint.mongodb import MongoDBSaver
    from pymongo import MongoClient

    parser = argparse.ArgumentParser(description="Pause or resume a Mongo-backed review thread.")
    parser.add_argument("phase", choices=("pause", "resume"))
    parser.add_argument("run_id", type=UUID)
    args = parser.parse_args()

    with MongoClient(os.environ["AO_MONGODB_URI"], serverSelectionTimeoutMS=5000) as client:
        client.admin.command("ping")
        checkpointer = MongoDBSaver(client, db_name="agent_orchestrator_checkpoint_smoke")
        exercise_phase(checkpointer, args.phase, args.run_id)


if __name__ == "__main__":
    main()
