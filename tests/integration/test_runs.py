import json
import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Barrier, Thread
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from langgraph.checkpoint.mongodb import MongoDBSaver
from pydantic import SecretStr
from pymongo.database import Database

from ecommerce_store_agent_orchestrator.api.app import create_app
from ecommerce_store_agent_orchestrator.infrastructure.mongo import Document, create_client
from ecommerce_store_agent_orchestrator.infrastructure.run_graph import build_preparation_graph, thread_config
from ecommerce_store_agent_orchestrator.infrastructure.settings import ApiSettings, MongoSettings

RUNS = "/api/v1/runs"
RUN_INPUT = {
    "baSessionId": "10000000-0000-4000-8000-000000000001",
    "baRunId": "10000000-0000-4000-8000-000000000002",
    "criterionIndex": 0,
}
ISSUER = "https://orchestrator-tests.example"
AUDIENCE = "agent-orchestrator-test"
BA_DRAFT_HASH = "a" * 64
QA_SESSION_ID = "10000000-0000-4000-8000-000000000005"
FEATURE = "Feature: Browse smartphones\n  Scenario: Open a listed phone\n    Then the details show Café\n"
FEATURE_HASH = sha256(FEATURE.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TokenIssuer:
    url: str
    key: rsa.RSAPrivateKey

    def headers(
        self, subject: str = "operator-1", scopes: str = "runs.start runs.read", **claims: object
    ) -> dict[str, str]:
        payload = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": subject,
            "exp": datetime.now(UTC) + timedelta(minutes=2),
            "scp": scopes,
            **claims,
        }
        token = jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": "integration-test"})
        return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def issuer() -> Iterator[TokenIssuer]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update({"kid": "integration-test", "use": "sig", "alg": "RS256"})
    body = json.dumps({"keys": [jwk]}).encode()

    class JwksHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), JwksHandler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield TokenIssuer(f"http://127.0.0.1:{server.server_port}/jwks.json", key)
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def api_settings(issuer: TokenIssuer) -> ApiSettings:
    return ApiSettings(
        mongodb_uri=SecretStr(os.environ.get("TEST_MONGODB_URI", "mongodb://127.0.0.1:27017")),
        mongodb_database=f"ao_test_{uuid4().hex}",
        auth_issuer=ISSUER,
        auth_audience=AUDIENCE,
        auth_jwks_url=issuer.url,
    )


@pytest.fixture
def database(api_settings: ApiSettings) -> Iterator[Database[Document]]:
    with create_client(api_settings) as client:
        client.admin.command("ping")
        try:
            yield client[api_settings.mongodb_database]
        finally:
            client.drop_database(api_settings.mongodb_database)


@pytest.fixture
def api(api_settings: ApiSettings, database: Database[Document]) -> Iterator[TestClient]:
    with TestClient(create_app(api_settings)) as client:
        yield client


def test_parallel_starts_register_one_run(api: TestClient, issuer: TokenIssuer, database: Database[Document]) -> None:
    headers = {**issuer.headers(), "Idempotency-Key": "parallel-start"}
    barrier = Barrier(8)

    def start(_request: int):
        barrier.wait(timeout=15)
        return api.post(RUNS, json=RUN_INPUT, headers=headers)

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(start, range(8)))

    assert [response.status_code for response in responses] == [202] * 8
    run_id = responses[0].json()["runId"]
    assert {response.json()["runId"] for response in responses} == {run_id}
    assert {response.headers["Location"] for response in responses} == {f"{RUNS}/{run_id}"}
    assert database.runs.count_documents({}) == 1
    assert database.runs.count_documents({"_id": run_id, "work.available_at": {"$exists": True}}) == 1

    conflict = api.post(RUNS, json={**RUN_INPUT, "criterionIndex": 1}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_key_reused"
    assert database.runs.count_documents({}) == 1

    other = api.post(
        RUNS, json=RUN_INPUT, headers={**issuer.headers("operator-2"), "Idempotency-Key": "parallel-start"}
    )
    assert other.status_code == 202
    assert other.json()["runId"] != run_id


def test_owned_run_reads_and_filtered_pagination(api: TestClient, issuer: TokenIssuer) -> None:
    headers = issuer.headers()
    created = [
        api.post(RUNS, json=RUN_INPUT, headers={**headers, "Idempotency-Key": f"page-{index}"}) for index in range(2)
    ]
    assert [response.status_code for response in created] == [202, 202]
    run_ids = {response.json()["runId"] for response in created}

    first = api.get(RUNS, params={"status": "queued", "limit": 1}, headers=headers).json()
    cursor = first["nextCursor"]
    second = api.get(RUNS, params={"status": "queued", "limit": 1, "cursor": cursor}, headers=headers).json()
    assert len(first["items"]) == len(second["items"]) == 1
    assert {first["items"][0]["runId"], second["items"][0]["runId"]} == run_ids
    assert "nextCursor" not in second
    assert api.get(RUNS, params={"status": "failed"}, headers=headers).json() == {"items": []}
    assert api.get(RUNS, params={"cursor": cursor}, headers=headers).status_code == 400

    run_path = created[0].headers["Location"]
    details = api.get(run_path, headers=headers)
    assert details.status_code == 200
    assert details.json()["input"] == RUN_INPUT
    assert details.json()["status"] == "queued"
    assert [event["status"] for event in details.json()["history"]] == ["queued"]

    other = issuer.headers("operator-2")
    assert api.get(run_path, headers=other).status_code == 404
    assert api.get(RUNS, params={"cursor": cursor, "status": "queued"}, headers=other).status_code == 400
    assert api.get(RUNS, headers=other).json() == {"items": []}


def test_auth_and_request_errors(api: TestClient, issuer: TokenIssuer) -> None:
    anonymous = api.get(RUNS)
    assert anonymous.status_code == 401
    assert anonymous.headers["WWW-Authenticate"] == "Bearer"
    assert anonymous.headers["Content-Type"] == "application/problem+json"
    assert api.get(RUNS, headers=issuer.headers(aud="different-api")).status_code == 401

    no_grant = api.post(
        RUNS, json=RUN_INPUT, headers={**issuer.headers(scopes="runs.read"), "Idempotency-Key": "read-only"}
    )
    assert no_grant.status_code == 403
    assert no_grant.json()["code"] == "forbidden"

    invalid = api.post(
        RUNS,
        json={**RUN_INPUT, "criterionIndex": "0"},
        headers={**issuer.headers(), "Idempotency-Key": "invalid"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_request"


_BEFORE_RESTART = """
from datetime import UTC, datetime, timedelta
from sys import argv

from langgraph.checkpoint.mongodb import MongoDBSaver

from ecommerce_store_agent_orchestrator.infrastructure.mongo import MongoRunRepository, create_client
from ecommerce_store_agent_orchestrator.infrastructure.run_graph import (
    build_preparation_graph, preparation_input, thread_config
)
from ecommerce_store_agent_orchestrator.infrastructure.settings import MongoSettings

settings = MongoSettings()
with create_client(settings) as client:
    repository = MongoRunRepository(client[settings.mongodb_database])
    claim = repository.claim_ready(datetime.now(UTC), timedelta(minutes=1))
    assert claim is not None
    assert str(claim.run.run_id) == argv[1]
    saver = MongoDBSaver(client, db_name=settings.mongodb_database)
    graph = build_preparation_graph(saver)
    config = thread_config(argv[1])
    interrupts = ["stage_handoff"] if argv[2] == "pending" else []
    graph.invoke(preparation_input(claim.run), config, durability="sync", interrupt_before=interrupts)
    assert graph.get_state(config).next == tuple(interrupts)
"""


@pytest.mark.parametrize("checkpoint", ["pending", "prepared"])
def test_fresh_worker_resumes_registered_work(
    api_settings: ApiSettings, issuer: TokenIssuer, database: Database[Document], checkpoint: str
) -> None:
    headers = issuer.headers()
    with TestClient(create_app(api_settings)) as api:
        registration = api.post(RUNS, json=RUN_INPUT, headers={**headers, "Idempotency-Key": "resume"})
        assert registration.status_code == 202
        run_id = registration.json()["runId"]
        run_path = registration.headers["Location"]

    worker_env = {
        **os.environ,
        "AO_MONGODB_URI": api_settings.mongodb_uri.get_secret_value(),
        "AO_MONGODB_DATABASE": database.name,
    }
    subprocess.run([sys.executable, "-c", _BEFORE_RESTART, run_id, checkpoint], env=worker_env, check=True, timeout=30)
    config = thread_config(run_id)
    graph = build_preparation_graph(MongoDBSaver(database.client, db_name=database.name))
    saved = graph.get_state(config)
    assert saved.values["run_id"] == run_id
    assert saved.next == (("stage_handoff",) if checkpoint == "pending" else ())
    assert database.runs.find_one({"_id": run_id})["status"] == "queued"

    # The first process exited without releasing its claim. Move its lease into the past.
    database.runs.update_one({"_id": run_id}, {"$set": {"work.available_at": datetime.now(UTC) - timedelta(seconds=1)}})
    worker = [sys.executable, "-m", "ecommerce_store_agent_orchestrator.worker", "--once"]
    subprocess.run(worker, env=worker_env, check=True, timeout=30)
    subprocess.run(worker, env=worker_env, check=True, timeout=30)

    with TestClient(create_app(api_settings)) as api:
        details = api.get(run_path, headers=headers)
        assert details.status_code == 200
        assert details.json()["status"] == "validating_handoff"
        assert [event["status"] for event in details.json()["history"]] == ["queued", "validating_handoff"]
        assert api.get(RUNS, params={"status": "queued"}, headers=headers).json() == {"items": []}

    resumed = graph.get_state(config)
    assert resumed.next == ()
    assert resumed.values["status"] == "validating_handoff"
    assert resumed.values["graph_version"] == 1
    assert database.runs.count_documents({"_id": run_id, "work": {"$exists": False}}) == 1
    if checkpoint == "prepared":
        assert resumed.config == saved.config


@dataclass
class AgentStub:
    url: str = ""
    ba_status: int = 200
    ba_method: str = "GET"
    draft_hash: str = BA_DRAFT_HASH
    proposal_hash: str = FEATURE_HASH
    proposal_snapshot: str | None = None
    lose_first_proposal: bool = False
    proposal_attempts: int = 0
    calls: list[tuple[str, str, dict]] = field(default_factory=list)

    def environment(self) -> dict[str, str]:
        return {
            "AO_BA_API_URL": f"{self.url}/ba/",
            "AO_BA_API_KEY": "ba-integration-key",
            "AO_QA_API_URL": f"{self.url}/qa/",
            "AO_QA_API_KEY": "qa-integration-key",
            "AO_SUPPORTED_HANDOFF": json.dumps({**RUN_INPUT, "draftSha256": BA_DRAFT_HASH}),
        }

    def respond(self, method: str, path: str, key: str | None, body: dict) -> tuple[int, dict]:
        self.calls.append((method, path, body))
        expected_key = "ba-integration-key" if path.startswith("/ba/") else "qa-integration-key"
        if key != expected_key:
            return 401, {}
        if method == "GET" and path == (
            f"/ba/api/v1/sessions/{RUN_INPUT['baSessionId']}/runs/{RUN_INPUT['baRunId']}/handoff"
        ):
            return self.ba_status, self.handoff()
        if method == "POST" and path == "/qa/api/v1/sessions":
            return 201, {"id": QA_SESSION_ID, **body}
        if method == "PUT" and path.startswith(f"/qa/api/v1/sessions/{QA_SESSION_ID}/e2e-proposals/"):
            self.proposal_attempts += 1
            if self.lose_first_proposal and self.proposal_attempts == 1:
                return 503, {}
            evidence = self.handoff()["draft"]
            source = evidence["sources"][0]
            return 200, {
                "provenance": {
                    **body,
                    "snapshotId": self.proposal_snapshot or evidence["snapshotId"],
                    "draftSha256": self.draft_hash,
                    "evidenceExchangeId": evidence["evidenceExchangeId"],
                    "jiraIssueKey": "DEMO-101",
                    "criterion": evidence["process"]["acceptanceCriteria"][0],
                    "sources": [{key: value for key, value in source.items() if key != "title"}],
                },
                "path": "features/catalog/browse-phone-details.feature",
                "feature": FEATURE,
                "featureSha256": self.proposal_hash,
            }
        return 404, {}

    def handoff(self) -> dict:
        return {
            "id": RUN_INPUT["baRunId"],
            "sessionId": RUN_INPUT["baSessionId"],
            "status": "published",
            "approvedAt": "2026-11-22T11:50:00Z",
            "publishedAt": "2026-11-22T11:55:00Z",
            "draftSha256": self.draft_hash,
            "request": {"service": "products", "method": self.ba_method, "path": "/mobile-phones"},
            "jiraIssue": {"key": "DEMO-101"},
            "draft": {
                "snapshotId": "demo-catalog-v1",
                "evidenceExchangeId": "10000000-0000-4000-8000-000000000004",
                "process": {
                    "acceptanceCriteria": [
                        {
                            "title": "View details for a listed phone",
                            "given": "The catalog is open",
                            "when": "I select a phone",
                            "then": "Its details match the selection",
                            "sourceIds": ["catalog-get"],
                        }
                    ]
                },
                "sources": [
                    {
                        "id": "catalog-get",
                        "title": "Products Catalog GET contract",
                        "service": "products",
                        "snapshotId": "demo-catalog-v1",
                        "path": "sources/openapi.json#/paths/~1mobile-phones/get",
                        "commitSha": "b" * 40,
                    }
                ],
            },
        }


@pytest.fixture
def agents() -> Iterator[AgentStub]:
    stub = AgentStub()

    class AgentHandler(BaseHTTPRequestHandler):
        def answer(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length)) if length else {}
            status, result = stub.respond(self.command, self.path, self.headers.get("X-Api-Key"), data)
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = answer
        do_POST = answer
        do_PUT = answer

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), AgentHandler) as server:
        stub.url = f"http://127.0.0.1:{server.server_port}"
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield stub
        finally:
            server.shutdown()
            thread.join()


def _worker_once(settings: MongoSettings, agents: AgentStub | None = None) -> None:
    env = {
        **os.environ,
        "AO_MONGODB_URI": settings.mongodb_uri.get_secret_value(),
        "AO_MONGODB_DATABASE": settings.mongodb_database,
        **(agents.environment() if agents is not None else {}),
    }
    worker = [sys.executable, "-m", "ecommerce_store_agent_orchestrator.worker", "--once"]
    subprocess.run(worker, env=env, check=True, timeout=30)


def _register_authoring_run(api: TestClient, headers: dict[str, str], criterion_index: int = 0) -> str:
    registration = api.post(
        RUNS,
        json={**RUN_INPUT, "criterionIndex": criterion_index},
        headers={**headers, "Idempotency-Key": "authoring-run"},
    )
    assert registration.status_code == 202
    return registration.headers["Location"]


def test_gated_authoring_retries_same_proposal_after_restart(
    api: TestClient, api_settings: ApiSettings, database: Database[Document], issuer: TokenIssuer, agents: AgentStub
) -> None:
    agents.lose_first_proposal = True
    headers = issuer.headers()
    path = _register_authoring_run(api, headers)
    run_id = path.rsplit("/", 1)[1]

    _worker_once(api_settings)
    assert api.get(path, headers=headers).json()["status"] == "validating_handoff"
    _worker_once(api_settings, agents)
    assert api.get(path, headers=headers).json()["status"] == "authoring_proposal"
    _worker_once(api_settings, agents)
    assert api.get(path, headers=headers).json()["status"] == "authoring_proposal"
    state = database.runs.find_one({"_id": run_id})
    assert state["qa_session_id"] == QA_SESSION_ID
    proposal_id = state["proposal_id"]

    database.runs.update_one({"_id": run_id}, {"$set": {"work.available_at": datetime.now(UTC) - timedelta(seconds=1)}})
    _worker_once(api_settings, agents)
    _worker_once(api_settings, agents)

    response = api.get(path, headers=headers).json()
    assert response["status"] == "awaiting_review"
    assert response["jiraIssueKey"] == "DEMO-101"
    assert response["handoff"]["snapshotId"] == "demo-catalog-v1"
    assert response["handoff"]["draftSha256"] == BA_DRAFT_HASH
    assert response["handoff"]["criterion"]["sourceIds"] == ["catalog-get"]
    assert response["handoff"]["sources"][0]["commitSha"] == "b" * 40
    assert response["review"]["feature"] == FEATURE
    assert response["review"]["featureSha256"] == FEATURE_HASH
    assert response["review"]["qaSessionId"] == QA_SESSION_ID
    assert response["review"]["proposalId"] == proposal_id
    assert [event["status"] for event in response["history"]] == [
        "queued",
        "validating_handoff",
        "authoring_proposal",
        "awaiting_review",
    ]
    qa_path = f"/qa/api/v1/sessions/{QA_SESSION_ID}/e2e-proposals/{proposal_id}"
    assert [method for method, _, _ in agents.calls] == ["GET", "POST", "PUT", "PUT"]
    assert agents.calls[1] == (
        "POST",
        "/qa/api/v1/sessions",
        {"operation": "author", "jiraIssueKey": "DEMO-101", "snapshotId": "demo-catalog-v1"},
    )
    assert agents.calls[2:] == [("PUT", qa_path, RUN_INPUT)] * 2

    graph = build_preparation_graph(MongoDBSaver(database.client, db_name=database.name))
    checkpoint = graph.get_state(thread_config(run_id))
    assert [item.value for item in checkpoint.interrupts] == [{"featureSha256": FEATURE_HASH}]


@pytest.mark.parametrize(
    ("ba_status", "ba_method", "draft_hash", "criterion_index", "failure"),
    [
        (409, "GET", BA_DRAFT_HASH, 0, "handoff_not_published"),
        (404, "GET", BA_DRAFT_HASH, 0, "handoff_not_found"),
        (200, "POST", BA_DRAFT_HASH, 0, "unsupported_criterion"),
        (200, "GET", BA_DRAFT_HASH, 1, "unsupported_criterion"),
        (200, "GET", "c" * 64, 0, "handoff_version_mismatch"),
    ],
)
def test_failed_handoff_stops_before_qa(
    api: TestClient,
    api_settings: ApiSettings,
    issuer: TokenIssuer,
    agents: AgentStub,
    ba_status: int,
    ba_method: str,
    draft_hash: str,
    criterion_index: int,
    failure: str,
) -> None:
    agents.ba_status = ba_status
    agents.ba_method = ba_method
    agents.draft_hash = draft_hash
    headers = issuer.headers()
    path = _register_authoring_run(api, headers, criterion_index)
    _worker_once(api_settings, agents)
    _worker_once(api_settings, agents)
    response = api.get(path, headers=headers).json()
    assert response["status"] == "failed"
    assert response["failure"]["code"] == failure
    assert response["failure"]["detail"]
    assert "review" not in response
    assert [method for method, _, _ in agents.calls] == ([] if criterion_index else ["GET"])


@pytest.mark.parametrize("mismatch", ["hash", "snapshot"])
def test_invalid_proposal_is_never_reviewable(
    api: TestClient, api_settings: ApiSettings, issuer: TokenIssuer, agents: AgentStub, mismatch: str
) -> None:
    if mismatch == "hash":
        agents.proposal_hash = "0" * 64
    else:
        agents.proposal_snapshot = "other-snapshot"
    headers = issuer.headers()
    path = _register_authoring_run(api, headers)
    for _ in range(3):
        _worker_once(api_settings, agents)
    response = api.get(path, headers=headers).json()
    assert response["status"] == "failed"
    assert response["failure"]["code"] == "invalid_qa_proposal"
    assert "review" not in response
