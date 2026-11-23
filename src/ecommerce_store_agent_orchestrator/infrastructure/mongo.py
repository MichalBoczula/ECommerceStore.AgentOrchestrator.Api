from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from ecommerce_store_agent_orchestrator.domain.evidence import HandoffSnapshot, ReviewSnapshot, RunFailure
from ecommerce_store_agent_orchestrator.domain.review import (
    CiEvidence,
    Publication,
    PublicationReceipt,
    PublicationRequest,
    ReviewDecision,
)
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
        decision=ReviewDecision.model_validate(document["decision"]) if "decision" in document else None,
        publication_request=(
            PublicationRequest.model_validate(document["publication_request"])
            if "publication_request" in document
            else None
        ),
        publication_receipt=(
            PublicationReceipt.model_validate(document["publication_receipt"])
            if "publication_receipt" in document
            else None
        ),
        publication=Publication.model_validate(document["publication"]) if "publication" in document else None,
        ci=CiEvidence.model_validate(document["ci"]) if "ci" in document else None,
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

    def claim_ready(
        self,
        now: datetime,
        lease: timedelta,
        *,
        with_authoring: bool = False,
        with_publication: bool = False,
        with_ci: bool = False,
    ) -> WorkClaim | None:
        token = uuid4()
        ready = {"work.available_at": {"$lte": now}}
        statuses = [RunStatus.QUEUED, RunStatus.REJECTED]
        unscheduled: list[RunStatus] = []
        if with_publication:
            statuses.append(RunStatus.PUBLISHING)
        if with_authoring:
            statuses.extend((RunStatus.VALIDATING_HANDOFF, RunStatus.AUTHORING_PROPOSAL))
            unscheduled.append(RunStatus.VALIDATING_HANDOFF)
        if with_ci:
            statuses.append(RunStatus.AWAITING_CI)
            unscheduled.append(RunStatus.AWAITING_CI)
        availability = (
            {"$or": [ready, {"status": {"$in": [status.value for status in unscheduled]}, "work": {"$exists": False}}]}
            if unscheduled
            else ready
        )
        document = self._runs.find_one_and_update(
            {"status": {"$in": [status.value for status in statuses]}, **availability},
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

    def record_decision(self, run: WorkflowRun, decision: ReviewDecision) -> RunStatus | None:
        review = run.review
        if review is None:
            return None
        status = RunStatus.PUBLISHING if decision.decision == "approve" else RunStatus.REJECTED
        at = decision.decided_at
        fields: Document = {
            "decision": decision.model_dump(mode="json"),
            "status": status.value,
            "updated_at": at,
            "work": {"available_at": at},
        }
        if status == RunStatus.PUBLISHING:
            fields["publication_request"] = PublicationRequest(
                qa_session_id=review.qa_session_id,
                proposal_id=review.proposal_id,
                feature_sha256=decision.feature_sha256,
            ).model_dump(mode="json")
        result = self._runs.update_one(
            {
                "_id": str(run.run_id),
                **_owner_query(run.owner),
                "status": RunStatus.AWAITING_REVIEW.value,
                "decision": {"$exists": False},
                "review.feature_sha256": decision.feature_sha256,
                "review.qa_session_id": str(review.qa_session_id),
                "review.proposal_id": str(review.proposal_id),
            },
            {"$set": fields, "$push": {"history": {"status": status.value, "at": at}}},
        )
        return status if result.modified_count == 1 else None

    def finish_rejection(self, claim: WorkClaim) -> bool:
        result = self._runs.update_one(self._claim_query(claim), {"$unset": {"work": ""}})
        return result.modified_count == 1

    def record_publication_receipt(self, claim: WorkClaim, receipt: PublicationReceipt) -> bool:
        result = self._runs.update_one(
            {**self._claim_query(claim), "publication_receipt": {"$exists": False}},
            {"$set": {"publication_receipt": receipt.model_dump(mode="json")}},
        )
        return result.modified_count == 1

    def finish_publication(self, claim: WorkClaim, publication: Publication, ci: CiEvidence, at: datetime) -> bool:
        status = RunStatus.AWAITING_CI if publication.outcome == "pull_request" else RunStatus.COMPLETED
        return self._advance(
            claim,
            status,
            at,
            {"publication": publication.model_dump(mode="json"), "ci": ci.model_dump(mode="json")},
            ready=status == RunStatus.AWAITING_CI,
        )

    def save_ci_evidence(self, claim: WorkClaim, ci: CiEvidence, at: datetime, next_poll_at: datetime | None) -> bool:
        evidence = ci.model_dump(mode="json")
        if next_poll_at is None:
            return self._advance(claim, RunStatus.COMPLETED, at, {"ci": evidence})
        result = self._runs.update_one(
            self._claim_query(claim),
            {"$set": {"ci": evidence, "updated_at": at, "work": {"available_at": next_poll_at}}},
        )
        return result.modified_count == 1

    def fail_work(self, claim: WorkClaim, failure: RunFailure, at: datetime) -> bool:
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
