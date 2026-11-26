# ECommerceStore Agent Orchestrator API

The workflow API for the ECommerceStore BA-to-QA review and publication process.
The API implements the start, list, detail, and decision operations in the
[OpenAPI contract](docs/openapi/agent-orchestrator.v1.yaml).

CI publishes the tested and scanned API and worker image to Docker Hub with the
commit SHA and `latest` tags on pushes to `main`. Manual runs on `main` publish
the commit SHA tag. Use `imageReference` from the `image-reference` artifact for
deployment. The image starts the API on port 8080 by default. Run
`python -m ecommerce_store_agent_orchestrator.worker` as the command of the
worker container using the same image and MongoDB settings.

## Run locally

Install Python 3.14 and uv, then start MongoDB 8.0.

~~~sh
docker compose up -d --wait mongo
uv sync --locked
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

## Enable BA and QA authoring

Set these values for the worker process.

| Variable | Value |
| --- | --- |
| `AO_BA_API_URL` and `AO_QA_API_URL` | Base URLs of the BA and QA APIs. |
| `AO_BA_API_KEY` and `AO_QA_API_KEY` | Service keys sent as `X-Api-Key` to the respective API. |
| `AO_SUPPORTED_HANDOFF` | JSON object with `baSessionId`, `baRunId`, `draftSha256`, and zero-based `criterionIndex`. |
| `AO_GITHUB_READ_TOKEN` | GitHub token with read access to E2E actions, pull requests, and contents. Public repository metadata may also be read without a token. |
| `AO_GITHUB_API_URL` | GitHub API base URL. Defaults to `https://api.github.com`. |

Use the published Products Catalog handoff for `GET /mobile-phones` or
`GET /mobile-phones/{id}` and pin the criterion that covers opening a listed phone and
verifying its details. Submit the same IDs and criterion index in the start request.
With the authoring variables unset, the local run stops at `validating_handoff`.
That run is eligible for validation when a configured worker starts.

Run details include the pinned BA criterion and source citations, the QA session and proposal IDs,
and the exact Gherkin and its SHA-256. `awaiting_review` indicates that the worker saved the review
checkpoint for that hash. Unsupported or unpublished handoffs report a `failure` in run details.
Temporary agent errors retry after the worker lease expires, with up to three attempts per stage.

## Review and publish

Send `POST /api/v1/runs/{runId}/decisions` with the run owner's bearer token and
the `runs.review` scope. The body has `decision` (`approve` or `reject`) and
`expectedFeatureSha256` from the run detail's review. Include a non-blank
`reason` for rejection.

HTTP `202` means the reviewer identity, feature hash, decision, and timestamp were
saved. A stale hash returns `409 stale_feature_hash`. A second decision returns
`409 review_closed`. The worker resumes the recorded decision after a restart.

Approval queues publication through QA using the saved session, proposal, and hash.
The worker verifies the feature bytes at the GitHub revision. A verified PR advances
the run to `awaiting_ci`. Identical content on `main` completes the run with a commit
permalink. Rejection finishes at review.

## Storefront CI evidence

The worker polls the Storefront E2E workflow for the published PR revision. A pending
observation is saved in MongoDB and scheduled again after 30 seconds. Set
`E2E_BASE_URL` in the E2E repository to a storefront reachable by Actions with a
populated catalog so the browser job runs.

Run details report the Gherkin check separately from the Playwright outcome. A
passing browser result requires the matching attempt's Allure result and a manifest
that binds the approved feature hash to the actual checkout. The API keeps the PR
head used by that attempt alongside the latest head observed. It links to the
GitHub artifact page. Reruns use a separate artifact name for each attempt.

## Worker traces

Set `AO_LANGSMITH_API_KEY` on the worker to enable LangSmith export. The default
`AO_LANGSMITH_PROJECT` is `ecommerce-store-agent-orchestrator`.
Set `AO_LANGSMITH_API_URL` to your LangSmith API base URL if you use a regional or
self-hosted endpoint.

Run details expose `traceCorrelationId`. Each exported worker attempt includes
that ID, its stage, attempt number, outcome and timestamps. Automatic graph
tracing is disabled in the worker. The explicit traces use empty inputs and
outputs. Request bodies, raw run and reviewer IDs, and exception messages stay
in their respective services. A failed trace export leaves the saved workflow
result available.

Follow the [catalog review demonstration](docs/demo.md) to rehearse transitions,
restarts, publication recovery and evidence navigation.

## Verify

With MongoDB 8.0 running, run `uv run pytest`. The integration tests create a fresh database
per test and use real worker processes, a local test JWKS endpoint, and HTTP fixtures for BA, QA, and GitHub.
Set `TEST_MONGODB_URI` to use another MongoDB instance.
