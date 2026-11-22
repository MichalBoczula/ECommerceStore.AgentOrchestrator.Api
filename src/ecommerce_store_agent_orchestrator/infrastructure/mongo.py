from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

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

    def claim_ready(self, now: datetime, lease: timedelta) -> WorkClaim | None:
        token = uuid4()
        document = self._runs.find_one_and_update(
            {"status": RunStatus.QUEUED.value, "work.available_at": {"$lte": now}},
            {"$set": {"work.available_at": now + lease, "work.lease_token": str(token)}},
            sort=[("work.available_at", ASCENDING), ("_id", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return WorkClaim(_read_run(document), token) if document is not None else None

    def mark_handoff_ready(self, claim: WorkClaim, at: datetime) -> bool:
        status = RunStatus.VALIDATING_HANDOFF.value
        result = self._runs.update_one(
            {
                "_id": str(claim.run.run_id),
                "status": RunStatus.QUEUED.value,
                "work.lease_token": str(claim.token),
            },
            {
                "$set": {"status": status, "updated_at": at},
                "$push": {"history": {"status": status, "at": at}},
                "$unset": {"work": ""},
            },
        )
        return result.modified_count == 1
