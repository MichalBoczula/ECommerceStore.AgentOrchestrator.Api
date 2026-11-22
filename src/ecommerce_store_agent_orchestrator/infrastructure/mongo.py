from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from ecommerce_store_agent_orchestrator.domain.evidence import HandoffSnapshot, ReviewSnapshot, RunFailure
from ecommerce_store_agent_orchestrator.domain.runs import (
    RunInput,
    RunOwner,
    RunPosition,
    RunStatus,
    StageEvent,
    WorkClaim,
    WorkflowRun,
)
from ecommerce_store_agent_orchestrator.infrastructure.settings import MongoSettings

type Document = dict[str, Any]


def create_client(settings: MongoSettings) -> MongoClient[Document]:
    return MongoClient(
        settings.mongodb_uri.get_secret_value(),
        tz_aware=True,
        w="majority",
        journal=True,
        readPreference="primary",
        readConcernLevel="majority",
        serverSelectionTimeoutMS=5_000,
        timeoutMS=10_000,
    )


def _owner_query(owner: RunOwner) -> Document:
    return {"owner.issuer": owner.issuer, "owner.subject": owner.subject}


def _read_run(document: Document) -> WorkflowRun:
    return WorkflowRun(
        run_id=UUID(document["_id"]),
        owner=RunOwner(document["owner"]["issuer"], document["owner"]["subject"]),
        input=RunInput(
            ba_session_id=UUID(document["input"]["ba_session_id"]),
            ba_run_id=UUID(document["input"]["ba_run_id"]),
            criterion_index=document["input"]["criterion_index"],
        ),
        status=RunStatus(document["status"]),
        created_at=document["created_at"],
        updated_at=document["updated_at"],
        history=tuple(StageEvent(RunStatus(stage["status"]), stage["at"]) for stage in document["history"]),
        graph_version=document.get("graph_version", 1),
        handoff=HandoffSnapshot.model_validate(document["handoff"]) if "handoff" in document else None,
        proposal_id=UUID(document["proposal_id"]) if "proposal_id" in document else None,
        qa_session_id=UUID(document["qa_session_id"]) if "qa_session_id" in document else None,
        review=ReviewSnapshot.model_validate(document["review"]) if "review" in document else None,
        failure=RunFailure.model_validate(document["failure"]) if "failure" in document else None,
    )


class MongoRunRepository:
    def __init__(self, database: Database[Document]) -> None:
        self._runs = database["runs"]

    def ensure_indexes(self) -> None:
        owner = [("owner.issuer", ASCENDING), ("owner.subject", ASCENDING)]
        ordering = [("created_at", DESCENDING), ("_id", DESCENDING)]
        self._runs.create_index([*owner, ("idempotency_key", ASCENDING)], unique=True, name="owner_idempotency")
        self._runs.create_index([*owner, *ordering], name="owner_runs")
        self._runs.create_index([*owner, ("status", ASCENDING), *ordering], name="owner_runs_by_status")
        self._runs.create_index(
            [("work.available_at", ASCENDING), ("_id", ASCENDING)],
            partialFilterExpression={"work.available_at": {"$exists": True}},
            name="ready_work",
        )

    def insert_or_get(self, run: WorkflowRun, idempotency_key: str) -> WorkflowRun:
        document = {
            "_id": str(run.run_id),
            "owner": {"issuer": run.owner.issuer, "subject": run.owner.subject},
            "input": {
                "ba_session_id": str(run.input.ba_session_id),
                "ba_run_id": str(run.input.ba_run_id),
                "criterion_index": run.input.criterion_index,
            },
            "status": run.status.value,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "graph_version": run.graph_version,
            "history": [{"status": stage.status.value, "at": stage.at} for stage in run.history],
            "idempotency_key": idempotency_key,
            "work": {"available_at": run.created_at},
        }
        try:
            self._runs.insert_one(document)
        except DuplicateKeyError:
            # Read the winning run with majority consistency before acknowledging a replay.
            existing = self._runs.find_one({**_owner_query(run.owner), "idempotency_key": idempotency_key})
            if existing is None:
                raise
            return _read_run(existing)
        return run

    def get(self, owner: RunOwner, run_id: UUID) -> WorkflowRun | None:
        document = self._runs.find_one({"_id": str(run_id), **_owner_query(owner)})
        return _read_run(document) if document is not None else None

    def page(
        self, owner: RunOwner, status: RunStatus | None, after: RunPosition | None, limit: int
    ) -> list[WorkflowRun]:
        query = _owner_query(owner)
        if status is not None:
            query["status"] = status.value
        if after is not None:
            query["$or"] = [
                {"created_at": {"$lt": after.created_at}},
                {"created_at": after.created_at, "_id": {"$lt": str(after.run_id)}},
            ]
        documents = self._runs.find(query).sort([("created_at", DESCENDING), ("_id", DESCENDING)]).limit(limit)
        return [_read_run(document) for document in documents]

    def claim_ready(self, now: datetime, lease: timedelta, *, with_authoring: bool = False) -> WorkClaim | None:
        token = uuid4()
        ready = {"work.available_at": {"$lte": now}}
        query: Document = {"status": RunStatus.QUEUED.value, **ready}
        if with_authoring:
            statuses = (RunStatus.QUEUED, RunStatus.VALIDATING_HANDOFF, RunStatus.AUTHORING_PROPOSAL)
            query = {
                "status": {"$in": [status.value for status in statuses]},
                "$or": [ready, {"status": RunStatus.VALIDATING_HANDOFF.value, "work": {"$exists": False}}],
            }
        document = self._runs.find_one_and_update(
            query,
            {
                "$set": {"work.available_at": now + lease, "work.lease_token": str(token)},
                "$inc": {"work.attempts": 1},
            },
            sort=[("work.available_at", ASCENDING), ("_id", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return WorkClaim(_read_run(document), token, document["work"]["attempts"]) if document is not None else None

    def mark_handoff_ready(self, claim: WorkClaim, at: datetime) -> bool:
        return self._advance(claim, RunStatus.VALIDATING_HANDOFF, at)

    def start_authoring(self, claim: WorkClaim, handoff: HandoffSnapshot, proposal_id: UUID, at: datetime) -> bool:
        return self._advance(
            claim,
            RunStatus.AUTHORING_PROPOSAL,
            at,
            {"handoff": handoff.model_dump(mode="json"), "proposal_id": str(proposal_id)},
            ready=True,
        )

    def record_qa_session(self, claim: WorkClaim, session_id: UUID) -> bool:
        result = self._runs.update_one(
            {**self._claim_query(claim), "qa_session_id": {"$exists": False}},
            {"$set": {"qa_session_id": str(session_id)}},
        )
        return result.modified_count == 1

    def record_review(self, claim: WorkClaim, review: ReviewSnapshot) -> bool:
        result = self._runs.update_one(
            {
                **self._claim_query(claim),
                "qa_session_id": str(review.qa_session_id),
                "proposal_id": str(review.proposal_id),
                "review": {"$exists": False},
            },
            {"$set": {"review": review.model_dump(mode="json")}},
        )
        return result.modified_count == 1

    def mark_review_ready(self, claim: WorkClaim, at: datetime) -> bool:
        return self._advance(claim, RunStatus.AWAITING_REVIEW, at)

    def fail_authoring(self, claim: WorkClaim, failure: RunFailure, at: datetime) -> bool:
        return self._advance(claim, RunStatus.FAILED, at, {"failure": failure.model_dump(mode="json")})

    @staticmethod
    def _claim_query(claim: WorkClaim) -> Document:
        return {
            "_id": str(claim.run.run_id),
            "status": claim.run.status.value,
            "work.lease_token": str(claim.token),
        }

    def _advance(
        self, claim: WorkClaim, status: RunStatus, at: datetime, data: Document | None = None, *, ready: bool = False
    ) -> bool:
        fields = {"status": status.value, "updated_at": at, **(data or {})}
        update: Document = {"$set": fields, "$push": {"history": {"status": status.value, "at": at}}}
        if ready:
            fields["work"] = {"available_at": at}
        else:
            update["$unset"] = {"work": ""}
        result = self._runs.update_one(
            self._claim_query(claim),
            update,
        )
        return result.modified_count == 1
