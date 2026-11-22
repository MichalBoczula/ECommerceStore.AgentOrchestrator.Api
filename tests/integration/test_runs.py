import json
import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from ecommerce_store_agent_orchestrator.infrastructure.settings import ApiSettings

RUNS = "/api/v1/runs"
RUN_INPUT = {
    "baSessionId": "10000000-0000-4000-8000-000000000001",
    "baRunId": "10000000-0000-4000-8000-000000000002",
    "criterionIndex": 0,
}
ISSUER = "https://orchestrator-tests.example"
AUDIENCE = "agent-orchestrator-test"


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
    assert database.runs.count_documents({"_id": run_id, "work": {"$exists": False}}) == 1
    if checkpoint == "prepared":
        assert resumed.config == saved.config
