import json
import os
import subprocess
import sys
from base64 import b64encode
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Barrier, Event, Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zipfile import ZipFile

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx2 import Client, MockTransport, Request, Response
from langgraph.checkpoint.mongodb import MongoDBSaver
from pydantic import SecretStr
from pymongo.database import Database

from ecommerce_store_agent_orchestrator.api.app import create_app
from ecommerce_store_agent_orchestrator.domain.review import Publication
from ecommerce_store_agent_orchestrator.infrastructure.github import GitHubClient
from ecommerce_store_agent_orchestrator.infrastructure.github_ci import GitHubCiReader
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
FEATURE_PATH = "features/catalog/browse-phone-details.feature"
E2E_REPOSITORY = "MichalBoczula/ECommerceStore.E2E"
PULL_SHA = "d" * 40
MAIN_SHA = "e" * 40
TESTED_SHA = "f" * 40
NEW_HEAD_SHA = "c" * 40
CI_RUN_ID = 9123
QA_BRANCH = f"qa/catalog-phone-details-{FEATURE_HASH[:24]}"


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

    worker_env = _worker_environment(api_settings)
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
    source_snapshot: str = "demo-catalog-v1"
    proposal_hash: str = FEATURE_HASH
    proposal_snapshot: str | None = None
    proposal_criterion_title: str | None = None
    lose_first_proposal: bool = False
    proposal_attempts: int = 0
    already_on_main: bool = False
    timeout_after_publication: bool = False
    timeout_simulated: bool = False
    fail_first_github_lookup: bool = False
    github_lookups: int = 0
    published_feature: str = FEATURE
    current_head: str = PULL_SHA
    tested_parent: str = PULL_SHA
    ci_runs: list[dict] = field(default_factory=list)
    ci_jobs: list[dict] = field(default_factory=list)
    ci_artifacts: list[dict] = field(default_factory=list)
    ci_archive: bytes = b""
    created_pulls: dict[tuple[str, str], int] = field(default_factory=dict)
    calls: list[tuple[str, str, dict]] = field(default_factory=list)
    capture_traces: bool = False
    traces: list[dict] = field(default_factory=list)

    def environment(self) -> dict[str, str]:
        env = {
            "AO_BA_API_URL": f"{self.url}/ba/",
            "AO_BA_API_KEY": "ba-integration-key",
            "AO_QA_API_URL": f"{self.url}/qa/",
            "AO_QA_API_KEY": "qa-integration-key",
            "AO_SUPPORTED_HANDOFF": json.dumps({**RUN_INPUT, "draftSha256": BA_DRAFT_HASH}),
            "AO_GITHUB_API_URL": f"{self.url}/github/",
            "AO_GITHUB_READ_TOKEN": "github-integration-key",
        }
        if self.capture_traces:
            env.update(
                AO_LANGSMITH_API_URL=f"{self.url}/langsmith/",
                AO_LANGSMITH_API_KEY="trace-integration-key",
                AO_LANGSMITH_PROJECT="orchestrator-rehearsal",
                LANGSMITH_TRACING="true",
                LANGSMITH_ENDPOINT=f"{self.url}/automatic-tracing/",
                LANGSMITH_API_KEY="automatic-tracing-key",
            )
        return env

    def respond(self, method: str, path: str, key: str | None, body: dict) -> tuple[int, dict | bytes]:
        if method == "POST" and path == "/langsmith/runs":
            if key != "trace-integration-key":
                return 401, {}
            self.traces.append(body)
            return 202, {}
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/github/"):
            return self.github_response(path) if key == "Bearer github-integration-key" else (401, {})
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
            if path.endswith("/pull-request"):
                return self.publish_response(path, body)
            self.proposal_attempts += 1
            if self.lose_first_proposal and self.proposal_attempts == 1:
                return 503, {}
            evidence = self.handoff()["draft"]
            source = evidence["sources"][0]
            criterion = evidence["process"]["acceptanceCriteria"][0]
            if self.proposal_criterion_title is not None:
                criterion = {**criterion, "title": self.proposal_criterion_title}
            return 200, {
                "provenance": {
                    **body,
                    "snapshotId": self.proposal_snapshot or evidence["snapshotId"],
                    "draftSha256": self.draft_hash,
                    "evidenceExchangeId": evidence["evidenceExchangeId"],
                    "jiraIssueKey": "DEMO-101",
                    "criterion": criterion,
                    "sources": [{key: value for key, value in source.items() if key != "title"}],
                },
                "path": FEATURE_PATH,
                "feature": FEATURE,
                "featureSha256": self.proposal_hash,
            }
        return 404, {}

    def publish_response(self, path: str, body: dict) -> tuple[int, dict]:
        if body != {"featureSha256": FEATURE_HASH}:
            return 409, {}
        if self.already_on_main:
            return 200, {"status": "already_current", "repository": E2E_REPOSITORY}
        key = (path, body["featureSha256"])
        number = self.created_pulls.setdefault(key, 17 + len(self.created_pulls))
        return 200, {
            "status": "pull_request",
            "repository": E2E_REPOSITORY,
            "number": number,
            "branch": QA_BRANCH,
        }

    def github_response(self, path: str) -> tuple[int, dict | bytes]:
        self.github_lookups += 1
        if self.fail_first_github_lookup and self.github_lookups == 1:
            return 503, {}
        repo = f"/github/repos/{E2E_REPOSITORY}"
        if path == f"{repo}/git/ref/heads/main":
            return 200, {"object": {"sha": MAIN_SHA}}
        if path == f"{repo}/pulls/17" and 17 in self.created_pulls.values():
            repo_info = {"full_name": E2E_REPOSITORY}
            return 200, {
                "number": 17,
                "head": {"ref": QA_BRANCH, "sha": self.current_head, "repo": repo_info},
                "base": {"ref": "main", "sha": MAIN_SHA, "repo": repo_info},
            }
        pinned = MAIN_SHA if self.already_on_main else PULL_SHA
        if path == f"{repo}/contents/{FEATURE_PATH}?ref={pinned}":
            return 200, {"encoding": "base64", "content": b64encode(self.published_feature.encode()).decode()}
        request = urlsplit(path)
        query = {"event": ["pull_request"], "head_sha": [PULL_SHA], "branch": [QA_BRANCH], "per_page": ["100"]}
        if request.path == f"{repo}/actions/workflows/ci.yml/runs" and parse_qs(request.query) == query:
            return 200, {"workflow_runs": self.ci_runs}
        if path == f"{repo}/actions/runs/{CI_RUN_ID}/attempts/2/jobs?per_page=100":
            return 200, {"jobs": self.ci_jobs}
        if path == f"{repo}/actions/runs/{CI_RUN_ID}/artifacts?name=storefront-test-results-attempt-2&per_page=100":
            return 200, {"artifacts": self.ci_artifacts}
        if path == f"{repo}/actions/artifacts/9502/zip":
            return 200, self.ci_archive
        if path == f"{repo}/commits/{TESTED_SHA}":
            return 200, {"sha": TESTED_SHA, "parents": [{"sha": MAIN_SHA}, {"sha": self.tested_parent}]}
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
                        "snapshotId": self.source_snapshot,
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
            header = "Authorization" if self.path.startswith("/github/") else "X-Api-Key"
            status, result = stub.respond(self.command, self.path, self.headers.get(header), data)
            if (
                stub.timeout_after_publication
                and not stub.timeout_simulated
                and self.path.endswith("/pull-request")
                and status == 200
            ):
                stub.timeout_simulated = True
                Event().wait(11)
                return
            body = result if isinstance(result, bytes) else json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/zip" if isinstance(result, bytes) else "application/json")
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


def _worker_environment(settings: MongoSettings, agents: AgentStub | None = None) -> dict[str, str]:
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith(("AO_", "LANGSMITH_", "LANGCHAIN_"))},
        "AO_MONGODB_URI": settings.mongodb_uri.get_secret_value(),
        "AO_MONGODB_DATABASE": settings.mongodb_database,
        **(agents.environment() if agents is not None else {}),
    }


def _worker_once(settings: MongoSettings, agents: AgentStub | None = None) -> None:
    worker = [sys.executable, "-m", "ecommerce_store_agent_orchestrator.worker", "--once"]
    subprocess.run(worker, env=_worker_environment(settings, agents), check=True, timeout=30)


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
    ("ba_status", "ba_method", "draft_hash", "criterion_index", "source_snapshot", "failure"),
    [
        (409, "GET", BA_DRAFT_HASH, 0, "demo-catalog-v1", "handoff_not_published"),
        (404, "GET", BA_DRAFT_HASH, 0, "demo-catalog-v1", "handoff_not_found"),
        (200, "POST", BA_DRAFT_HASH, 0, "demo-catalog-v1", "unsupported_criterion"),
        (200, "GET", BA_DRAFT_HASH, 1, "demo-catalog-v1", "unsupported_criterion"),
        (200, "GET", "c" * 64, 0, "demo-catalog-v1", "handoff_version_mismatch"),
        (200, "GET", BA_DRAFT_HASH, 0, "another-snapshot", "handoff_version_mismatch"),
    ],
    ids=["unpublished", "missing", "wrong-operation", "wrong-index", "changed-draft", "wrong-source-snapshot"],
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
    source_snapshot: str,
    failure: str,
) -> None:
    agents.ba_status = ba_status
    agents.ba_method = ba_method
    agents.draft_hash = draft_hash
    agents.source_snapshot = source_snapshot
    headers = issuer.headers()
    path = _register_authoring_run(api, headers, criterion_index)
    _worker_once(api_settings, agents)
    _worker_once(api_settings, agents)
    response = api.get(path, headers=headers).json()
    assert response["status"] == "failed"
    assert response["failure"]["code"] == failure
    assert response["failure"]["detail"]
    assert "review" not in response
    assert not agents.created_pulls
    assert [method for method, _, _ in agents.calls] == ([] if criterion_index else ["GET"])


@pytest.mark.parametrize("mismatch", ["hash", "snapshot", "criterion"])
def test_invalid_proposal_is_never_reviewable(
    api: TestClient, api_settings: ApiSettings, issuer: TokenIssuer, agents: AgentStub, mismatch: str
) -> None:
    if mismatch == "hash":
        agents.proposal_hash = "0" * 64
    elif mismatch == "snapshot":
        agents.proposal_snapshot = "other-snapshot"
    else:
        agents.proposal_criterion_title = "Submit a different catalog operation"
    headers = issuer.headers(scopes="runs.start runs.read runs.review")
    path = _register_authoring_run(api, headers)
    for _ in range(3):
        _worker_once(api_settings, agents)
    response = api.get(path, headers=headers).json()
    assert response["status"] == "failed"
    assert response["failure"]["code"] == "invalid_qa_proposal"
    assert "review" not in response
    refused = api.post(
        f"{path}/decisions",
        json={"decision": "approve", "expectedFeatureSha256": FEATURE_HASH},
        headers=headers,
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "review_closed"
    assert not agents.created_pulls


def _stage_review(api: TestClient, settings: MongoSettings, agents: AgentStub, headers: dict[str, str]) -> str:
    path = _register_authoring_run(api, headers)
    for _ in range(3):
        _worker_once(settings, agents)
    assert api.get(path, headers=headers).json()["status"] == "awaiting_review"
    return path


def test_review_access_and_one_concurrent_decision(
    api: TestClient, api_settings: ApiSettings, issuer: TokenIssuer, agents: AgentStub
) -> None:
    headers = issuer.headers(scopes="runs.start runs.read runs.review")
    path = _stage_review(api, api_settings, agents, headers)
    decision_path = f"{path}/decisions"
    approval = {"decision": "approve", "expectedFeatureSha256": FEATURE_HASH}
    assert api.post(decision_path, json=approval, headers=issuer.headers()).status_code == 403
    other = issuer.headers(subject="operator-2", scopes="runs.review")
    assert api.post(decision_path, json=approval, headers=other).status_code == 404
    assert api.post(decision_path, json={**approval, "decision": "reject"}, headers=headers).status_code == 400
    stale = api.post(decision_path, json={**approval, "expectedFeatureSha256": "0" * 64}, headers=headers)
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_feature_hash"

    barrier = Barrier(6)

    def submit(_attempt: int):
        barrier.wait(timeout=15)
        return api.post(decision_path, json=approval, headers=headers)

    with ThreadPoolExecutor(max_workers=6) as executor:
        decisions = list(executor.map(submit, range(6)))

    assert sorted(response.status_code for response in decisions) == [202, 409, 409, 409, 409, 409]
    assert {response.json()["code"] for response in decisions if response.status_code == 409} == {"review_closed"}
    run = api.get(path, headers=headers).json()
    assert run["status"] == "publishing"
    assert run["decision"]["reviewer"] == {"issuer": ISSUER, "subject": "operator-1"}
    assert run["decision"]["featureSha256"] == FEATURE_HASH
    assert datetime.fromisoformat(run["decision"]["decidedAt"]).tzinfo is not None
    assert sum(event["status"] == "publishing" for event in run["history"]) == 1
    assert not agents.created_pulls


def test_rejection_resumes_checkpoint_and_finishes_without_publication(
    api: TestClient, api_settings: ApiSettings, database: Database[Document], issuer: TokenIssuer, agents: AgentStub
) -> None:
    headers = issuer.headers(scopes="runs.start runs.read runs.review")
    path = _stage_review(api, api_settings, agents, headers)
    run_id = path.rsplit("/", 1)[1]
    rejection = {"decision": "reject", "expectedFeatureSha256": FEATURE_HASH, "reason": "The scenario needs a retry."}
    response = api.post(f"{path}/decisions", json=rejection, headers=headers)
    assert response.status_code == 202
    assert response.json() == {"runId": run_id, "status": "rejected"}
    _worker_once(api_settings)

    run = api.get(path, headers=headers).json()
    assert run["status"] == "rejected"
    assert run["decision"]["reason"] == rejection["reason"]
    assert sum(event["status"] == "rejected" for event in run["history"]) == 1
    assert "publication" not in run
    assert [method for method, _, _ in agents.calls] == ["GET", "POST", "PUT"]
    assert database.runs.count_documents({"_id": run_id, "work": {"$exists": False}}) == 1
    graph = build_preparation_graph(MongoDBSaver(database.client, db_name=database.name))
    snapshot = graph.get_state(thread_config(run_id))
    assert snapshot.values["review_decision"] == "reject"
    assert not snapshot.next and not snapshot.interrupts


def test_publication_recovers_one_pull_after_timeout_and_github_retry(
    api: TestClient, api_settings: ApiSettings, database: Database[Document], issuer: TokenIssuer, agents: AgentStub
) -> None:
    agents.timeout_after_publication = True
    agents.fail_first_github_lookup = True
    agents.capture_traces = True
    headers = issuer.headers(scopes="runs.start runs.read runs.review")
    path = _stage_review(api, api_settings, agents, headers)
    run_id = path.rsplit("/", 1)[1]
    staged = api.get(path, headers=headers).json()
    review = staged["review"]
    trace_id = staged["traceCorrelationId"]
    approval = {"decision": "approve", "expectedFeatureSha256": FEATURE_HASH}
    assert api.post(f"{path}/decisions", json=approval, headers=headers).status_code == 202
    stored = database.runs.find_one({"_id": run_id})
    assert stored["publication_request"] == {
        "qa_session_id": QA_SESSION_ID,
        "proposal_id": review["proposalId"],
        "feature_sha256": FEATURE_HASH,
    }
    assert not agents.created_pulls

    _worker_once(api_settings, agents)
    assert agents.timeout_simulated
    assert len(agents.created_pulls) == 1
    assert api.get(path, headers=headers).json()["status"] == "publishing"
    for _ in range(2):
        database.runs.update_one(
            {"_id": run_id}, {"$set": {"work.available_at": datetime.now(UTC) - timedelta(seconds=1)}}
        )
        _worker_once(api_settings, agents)

    run = api.get(path, headers=headers).json()
    assert run["status"] == "awaiting_ci"
    assert run["publication"]["pullRequestNumber"] == 17
    assert run["publication"]["commitSha"] == PULL_SHA
    assert run["ci"] == {"status": "pending", "gherkinCheckStatus": "pending"}
    assert database.runs.count_documents({"_id": run_id, "work.available_at": {"$exists": True}}) == 1
    qa_path = f"/qa/api/v1/sessions/{QA_SESSION_ID}/e2e-proposals/{review['proposalId']}/pull-request"
    assert [call for call in agents.calls if call[1].endswith("/pull-request")] == [
        ("PUT", qa_path, {"featureSha256": FEATURE_HASH})
    ] * 2
    assert agents.created_pulls == {(qa_path, FEATURE_HASH): 17}
    assert agents.github_lookups == 3

    # Runs published by AO-04 had no scheduled CI work.
    database.runs.update_one({"_id": run_id}, {"$unset": {"work": ""}})
    _worker_once(api_settings, agents)
    pending = api.get(path, headers=headers).json()
    assert pending["status"] == "awaiting_ci"
    assert pending["ci"]["status"] == "pending"
    assert database.runs.find_one({"_id": run_id})["work"]["available_at"] > datetime.now(UTC)

    _configure_ci_fixture(agents)
    agents.current_head = NEW_HEAD_SHA
    database.runs.update_one({"_id": run_id}, {"$set": {"work.available_at": datetime.now(UTC) - timedelta(seconds=1)}})
    _worker_once(api_settings, agents)
    completed = api.get(path, headers=headers).json()
    assert completed["status"] == "completed"
    assert completed["ci"]["status"] == "passed"
    assert completed["ci"]["observedHeadSha"] == PULL_SHA
    assert completed["ci"]["testedCommitSha"] == TESTED_SHA
    assert completed["ci"]["currentHeadSha"] == NEW_HEAD_SHA
    assert completed["ci"]["runAttempt"] == 2
    assert completed["ci"]["allureArtifact"]["id"] == 9502
    assert database.runs.count_documents({"_id": run_id, "work": {"$exists": False}}) == 1
    assert completed["traceCorrelationId"] == trace_id
    assert not any(path.startswith("/automatic-tracing/") for _, path, _ in agents.calls)
    metadata = [trace["extra"]["metadata"] for trace in agents.traces]
    assert {item["correlation_id"] for item in metadata} == {trace_id}
    assert (
        trace_id == sha256(b"ecommerce-store-agent-orchestrator:" + bytes.fromhex(run_id.replace("-", ""))).hexdigest()
    )
    assert [(item["stage"], item["outcome"]) for item in metadata] == [
        ("queued", "processed"),
        ("validating_handoff", "processed"),
        ("authoring_proposal", "processed"),
        ("publishing", "retrying"),
        ("publishing", "retrying"),
        ("publishing", "processed"),
        ("awaiting_ci", "processed"),
        ("awaiting_ci", "processed"),
    ]
    assert all(trace["inputs"] == trace["outputs"] == {} for trace in agents.traces)
    raw_trace = json.dumps(agents.traces)
    sensitive_values = (run_id, QA_SESSION_ID, review["proposalId"], "operator-1", "DEMO-101", "integration-key")
    assert all(value not in raw_trace for value in sensitive_values)
    assert all(
        datetime.fromisoformat(trace["start_time"]) <= datetime.fromisoformat(trace["end_time"])
        for trace in agents.traces
    )
    print(
        json.dumps(
            {
                "runId": run_id,
                "traceCorrelationId": trace_id,
                "createdPullRequests": len(agents.created_pulls),
                "stages": [event["status"] for event in completed["history"]],
                "observedHeadSha": completed["ci"]["observedHeadSha"],
                "playwright": completed["ci"]["status"],
                "workflowRunId": completed["ci"]["workflowRunId"],
                "attempt": completed["ci"]["runAttempt"],
                "allureArtifact": completed["ci"]["allureArtifact"]["name"],
            },
            indent=2,
        )
    )


@pytest.mark.parametrize("matching_feature", [True, False])
def test_already_on_main_requires_the_exact_feature_at_a_pinned_commit(
    api: TestClient, api_settings: ApiSettings, issuer: TokenIssuer, agents: AgentStub, matching_feature: bool
) -> None:
    agents.already_on_main = True
    if not matching_feature:
        agents.published_feature = "Feature: Unreviewed change\n"
    headers = issuer.headers(scopes="runs.start runs.read runs.review")
    path = _stage_review(api, api_settings, agents, headers)
    approval = {"decision": "approve", "expectedFeatureSha256": FEATURE_HASH}
    assert api.post(f"{path}/decisions", json=approval, headers=headers).status_code == 202
    _worker_once(api_settings, agents)
    run = api.get(path, headers=headers).json()
    assert not agents.created_pulls
    if matching_feature:
        assert run["status"] == "completed"
        assert run["publication"] == {
            "outcome": "already_current",
            "repository": E2E_REPOSITORY,
            "commitSha": MAIN_SHA,
            "url": f"https://github.com/{E2E_REPOSITORY}/blob/{MAIN_SHA}/{FEATURE_PATH}",
        }
        assert run["ci"] == {
            "status": "not_applicable",
            "gherkinCheckStatus": "not_run",
            "reasonCode": "already_on_main",
        }
    else:
        assert run["status"] == "failed"
        assert run["failure"]["code"] == "publication_revision_mismatch"
        assert "publication" not in run


def _configure_ci_fixture(agents: AgentStub, case: str = "passed") -> None:
    outcome = "failure" if case == "failed" else "success"
    agents.ci_runs = [
        {
            "id": CI_RUN_ID,
            "run_number": 42,
            "run_attempt": 2,
            "event": "pull_request",
            "status": "in_progress" if case == "pending" else "completed",
            "head_sha": PULL_SHA,
            "head_branch": QA_BRANCH,
            "display_title": f"Storefront PR #{18 if case == 'different_pr' else 17} at {PULL_SHA}",
            "pull_requests": [],
        }
    ]
    agents.ci_jobs = [
        {
            "id": 9101,
            "name": "check",
            "status": "completed",
            "conclusion": "failure" if case == "check_failed" else "success",
        },
        {
            "id": 9102,
            "name": "storefront",
            "status": "completed",
            "conclusion": "skipped" if case in ("job_skipped", "check_failed") else outcome,
            "steps": [] if case == "setup_failed" else [{"name": "Run storefront Playwright", "conclusion": outcome}],
        },
    ]
    if case == "interrupted":
        agents.ci_jobs[1]["steps"][0].update({"conclusion": "cancelled", "started_at": "2026-11-22T21:00:00Z"})
    agents.ci_artifacts = [{"id": 9501, "name": "storefront-test-results", "expired": False}]
    if case != "old_artifact":
        agents.ci_artifacts.append({"id": 9502, "name": "storefront-test-results-attempt-2", "expired": False})

    entries = BytesIO()
    with ZipFile(entries, "w") as archive:
        archive.writestr(
            "storefront-evidence.json",
            json.dumps(
                {
                    "schemaVersion": 1,
                    "prNumber": 17,
                    "prHeadSha": PULL_SHA,
                    "testedCommitSha": TESTED_SHA,
                    "featureSha256": FEATURE_HASH if case != "wrong_hash" else "0" * 64,
                    "workflowRunId": CI_RUN_ID,
                    "runAttempt": 1 if case == "wrong_attempt" else 2,
                    "playwrightOutcome": outcome,
                }
            ),
        )
        scenario_status = {"failed": "failed", "scenario_skipped": "skipped"}.get(case, "passed")
        scenario_id = "AnotherScenario" if case == "other_scenario" else "E2E_Catalog_BrowsePhoneDetails_01"
        archive.writestr(
            "allure-results/demo-result.json",
            json.dumps(
                {
                    "status": scenario_status,
                    "stage": "finished",
                    "labels": [{"name": "scenarioId", "value": scenario_id}],
                }
            ),
        )
    agents.ci_archive = entries.getvalue()
    if case == "wrong_merge":
        agents.tested_parent = NEW_HEAD_SHA


@pytest.mark.parametrize(
    ("case", "result", "gherkin", "reason"),
    [
        ("passed", "passed", "passed", None),
        ("failed", "failed", "passed", "playwright_failed"),
        ("pending", "pending", "passed", None),
        ("different_pr", "pending", "pending", None),
        ("job_skipped", "not_run", "passed", "storefront_skipped"),
        ("check_failed", "not_run", "failed", "storefront_skipped"),
        ("setup_failed", "not_run", "passed", "playwright_not_run"),
        ("interrupted", "evidence_missing", "passed", "playwright_interrupted"),
        ("scenario_skipped", "not_run", "passed", "scenario_skipped"),
        ("old_artifact", "evidence_missing", "passed", "allure_artifact_missing"),
        ("other_scenario", "evidence_missing", "passed", "scenario_result_missing"),
        ("wrong_hash", "evidence_missing", "passed", "revision_evidence_mismatch"),
        ("wrong_attempt", "evidence_missing", "passed", "revision_evidence_mismatch"),
        ("wrong_merge", "evidence_missing", "passed", "revision_evidence_mismatch"),
    ],
)
def test_ci_result_requires_the_published_revision_and_matching_allure_result(
    case: str, result: str, gherkin: str, reason: str | None
) -> None:
    agents = AgentStub(current_head=NEW_HEAD_SHA)
    agents.created_pulls[("published", FEATURE_HASH)] = 17
    _configure_ci_fixture(agents, case)

    def respond(request: Request) -> Response:
        status, body = agents.github_response(f"/github{request.url.raw_path.decode()}")
        if isinstance(body, bytes):
            return Response(status, content=body)
        return Response(status, json=body)

    publication = Publication(
        outcome="pull_request",
        repository=E2E_REPOSITORY,
        url=f"https://github.com/{E2E_REPOSITORY}/pull/17",
        commit_sha=PULL_SHA,
        pull_request_number=17,
        branch=QA_BRANCH,
    )
    with Client(base_url="https://github-ci.example", transport=MockTransport(respond)) as client:
        evidence = GitHubCiReader(GitHubClient(client)).observe(publication, FEATURE_HASH)
    assert evidence.status == result
    assert evidence.gherkin_check_status == gherkin
    assert evidence.reason_code == reason
    assert evidence.current_head_sha == NEW_HEAD_SHA
    if result in ("passed", "failed"):
        assert evidence.workflow_run_id == CI_RUN_ID
        assert evidence.run_attempt == 2
        assert evidence.storefront_job_id == 9102
        assert evidence.observed_head_sha == PULL_SHA
        assert evidence.tested_commit_sha == TESTED_SHA
        assert evidence.allure_artifact is not None
        assert evidence.allure_artifact.name == "storefront-test-results-attempt-2"
        assert evidence.allure_artifact.id == 9502
