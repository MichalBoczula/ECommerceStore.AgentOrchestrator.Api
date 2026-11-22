# ECommerceStore Agent Orchestrator API

The workflow API for the ECommerceStore BA-to-QA review and publication process.
AO-02 implements the start, list, and detail operations in the
[OpenAPI contract](docs/openapi/agent-orchestrator.v1.yaml).

## Run locally

Install Python 3.14 and uv, then start MongoDB 8.0.

~~~sh
docker compose up -d --wait mongo
uv sync
~~~

Set `AO_AUTH_ISSUER`, `AO_AUTH_AUDIENCE`, and `AO_AUTH_JWKS_URL` from your configured identity provider.
Use an RS256 bearer access token for this API with delegated `runs.start` and `runs.read` scopes.
The caller's issuer and subject scope idempotency keys and run reads.

~~~sh
uv run uvicorn ecommerce_store_agent_orchestrator.api.app:create_app --factory --port 8000
~~~

Start one worker process in a second terminal. The API and worker use `AO_MONGODB_URI`
(default `mongodb://localhost:27017`) and `AO_MONGODB_DATABASE`
(default `ecommerce_store_agent_orchestrator`).

~~~sh
uv run python -m ecommerce_store_agent_orchestrator.worker
~~~

The worker polls every second by default. `AO_WORKER_POLL_SECONDS` controls the interval and
`AO_WORKER_LEASE_SECONDS` controls the claim duration (default 30 seconds).
Use `--once` to process one due run and exit.

## Start and read a run

With your access token in `AO_ACCESS_TOKEN`, send the synthetic example below.

~~~sh
curl -i -X POST http://localhost:8000/api/v1/runs \
  -H "Authorization: Bearer $AO_ACCESS_TOKEN" \
  -H "Idempotency-Key: catalog-demo-01" \
  -H "Content-Type: application/json" \
  -d '{
    "baSessionId": "10000000-0000-4000-8000-000000000001",
    "baRunId": "10000000-0000-4000-8000-000000000002",
    "criterionIndex": 0
  }'
~~~

The response is `202` with a `runId` and a `Location` for its detail endpoint.
Read the run at that location using the bearer token or list runs at `GET /api/v1/runs`.
The list accepts `status`, `limit`, and the returned `nextCursor` as `cursor`.

The API writes the run and pending work together. The worker prepares the same run UUID as
a LangGraph thread and advances it from `queued` to `validating_handoff`.
AO-03 adds live BA validation and QA authoring at that stage.

## Verify

With MongoDB 8.0 running, run `uv run pytest`. The integration tests create a fresh database
per test and use real worker processes and a local test JWKS endpoint.
Set `TEST_MONGODB_URI` to use another MongoDB instance.
