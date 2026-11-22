# ADR 0001 · Run state and integration boundaries

**Status** Proposed for AO-01 (KAN-12)

## Context

The first workflow takes a published BA handoff for the storefront catalog-to-phone-details journey through QA authoring, human review, PR publication, and Playwright evidence. The API and Angular panel share the [versioned OpenAPI contract](../openapi/agent-orchestrator.v1.yaml).

## Ownership

| Component | Authoritative data and responsibilities |
| --- | --- |
| BA | Published handoff, `draftSha256`, approval, knowledge snapshot, criteria, and source citations. |
| QA | Author session, proposal, exact UTF-8 Gherkin, `featureSha256`, and publication receipt. QA owns credentials that can publish to E2E. |
| GitHub E2E | PR revision, workflow runs and attempts, executed job results, and test artifacts. |
| Orchestrator API and MongoDB | Run identity, input and version references, reviewer decision, work scheduling, timeline, and verified evidence references. The panel reads the review snapshot from the run. |
| LangGraph and MongoDB checkpointer | Per-thread progress and pending interrupts. The application stores the `runId` and uses its canonical UUID string as `configurable.thread_id`. |
| Angular panel | Run list, timeline, Gherkin and citations, review actions, and revision-specific CI evidence. Use the Knowledge Assistant Web feature layers, NgRx, and Kiota transport boundary. |
| LangSmith | Stage, duration, outcome, and a pseudonymous correlation ID. Send traces with an explicit metadata allowlist. Keep source text, credentials, reviewer identities, and raw integration payloads inside the application boundary. |

## Durable execution and review

Use Python 3.14, FastAPI, and a separate LangGraph worker. BA and QA use FastAPI application, domain, and infrastructure layers with Pydantic contracts and an explicit Mongo adapter. The orchestrator follows those boundaries. FastAPI accepts and reads runs, and the worker advances the graph using durable work stored in MongoDB.

Use the `MongoDBSaver` from `langgraph-checkpoint-mongodb` with MongoDB 8.0, the database version used by QA locally. Use an orchestrator database with separate run and checkpoint collections. The worker calls the synchronous graph and saver APIs. The API owns its own Mongo client. Pin the graph and saver versions together and use synchronous graph durability at the review boundary. Keep the graph version with each run and preserve node names and interrupt ordering for threads that are still active.

1. `POST /api/v1/runs` atomically inserts the queued run and pending work. A unique index on the authenticated caller and `Idempotency-Key` makes identical validated requests return the same `runId`. Reusing the key with different input returns `409 idempotency_key_reused`. Retain the key for as long as the run exists.
2. The worker reads the BA handoff, validates the selected criterion and pins the source version. It saves a `proposalId` before authoring. It pins the first confirmed QA author session before sending a proposal. If a session creation response is lost, a retry can leave an unused QA session. Proposal and publication retries use the durable `qaSessionId` and `proposalId` once assigned.
3. The worker checks the returned QA provenance against the pinned BA handoff and checks the hash of the exact Gherkin bytes. It persists the presentation snapshot and uses a side-effect-free review node with `interrupt()`. The run becomes reviewable once the pending interrupt is durable. After a restart, the worker uses the saved review and checkpoint to complete this transition.
4. The API checks the reviewer grant and atomically matches `runId`, `awaiting_review`, and `expectedFeatureSha256`. The same write records the server-derived reviewer identity, decision time, accepted hash, and pending resume work. Approval also records the publication inputs. A hash mismatch returns `409 stale_feature_hash`. A decision for a closed review returns `409 review_closed`. Rejection requires a reason and immediately shows `rejected` in the API.
5. The worker reads the persisted decision and dispatches `Command(resume=...)` to the same thread. The rejected branch ends the graph at review. The approved branch proceeds to publication. After a restart, the worker inspects the latest checkpoint before resuming and reconciles an advanced checkpoint with stored receipts and state. Integration nodes reuse persisted request identities on replay.

For MVP, run one graph worker deployment and prevent overlap during replacement. The worker claims due work atomically with a lease and uses its claim token to guard run writes. API replicas can accept concurrent starts and decisions. A paused review holds no worker lease. Expired claims and pending work are eligible for recovery after restart, including finishing a recorded rejection. Track retries and the next CI poll in the run record so the worker can release the claim between attempts.

## BA handoff and QA authoring

The first supported journey is **open the smartphone catalog, select a listed phone, and verify its details and specifications**. Use a published `products` handoff for `GET /mobile-phones` as the demo target. QA also accepts `GET /mobile-phones/{id}` with the actual path parameter name from the handoff. Select a sourced criterion that explicitly covers the journey. QA renders a fixed storefront scenario for this path, so the first deployment pins the approved `(baSessionId, baRunId, draftSha256, criterionIndex)` for that criterion. This prevents an unrelated criterion on the same route from being represented as covered.

| Step | Existing contract |
| --- | --- |
| Read BA handoff | `GET /api/v1/sessions/{baSessionId}/runs/{baRunId}/handoff` returns published status, approval and publication timestamps, Jira issue, draft hash, snapshot, evidence exchange, and criteria. Check the IDs and version and resolve the selected criterion's citations. A missing or unapproved handoff, invalid source, or unsupported criterion fails before creating a QA proposal. |
| Register QA session | `POST /api/v1/sessions` with `operation: author`, `jiraIssueKey`, and `snapshotId` from the handoff. |
| Author proposal | `PUT /api/v1/sessions/{qaSessionId}/e2e-proposals/{proposalId}` with `baSessionId`, `baRunId`, and the zero-based `criterionIndex`. Compare `provenance` with the pinned BA data. Store the exact returned `feature` and `featureSha256`. |
| Publish proposal | `PUT /api/v1/sessions/{qaSessionId}/e2e-proposals/{proposalId}/pull-request` with the accepted `featureSha256`. Reuse the tuple following a timeout. QA revalidates the BA provenance and recovers its PR by the deterministic branch and feature hash marker. |

The OpenAPI examples use synthetic handoff identifiers. The checked-in BA lab demonstrates `POST /mobile-phones`, and the E2E repo has the checked-in storefront scenario. A live published BA handoff for the selected GET operation must supply the demo's actual IDs, hash, and criterion. Confirm it through the authenticated handoff endpoint and register the observed tuple in the demo configuration.

## Publication and CI evidence

Capture GitHub's PR head SHA as soon as QA returns `pull_request` and verify the approved feature at that exact revision before recording it. QA's receipt contains the PR number and branch. Query GitHub by repository and PR number and read `features/catalog/browse-phone-details.feature` at the returned SHA. Stop the run with `publication_revision_mismatch` if the bytes differ from the approved hash. If QA returns `already_current`, verify the feature at a resolved `main` commit and record `already_current` with a pinned file link. Its CI status is `not_applicable`.

Observe `Storefront E2E` runs for the published PR and head SHA. Keep the workflow run ID and attempt number with the `storefront` job and artifact. The workflow currently checks out the GitHub-selected commit, so a PR job may test a merge commit. AO-05 needs per-attempt evidence that records the PR head SHA, actual checkout SHA, workflow run and attempt, and the tested feature hash. Verify the artifact `storefront-test-results` and the selected scenario in `TestResults/allure-results` from that same attempt. Keep the published head and tested commit separately. If the PR moves, show the immutable result with its recorded head and the latest observed PR head.

| Browser result | Condition |
| --- | --- |
| `pending` | Waiting for the correlated workflow or Playwright outcome. |
| `passed` | The matching `storefront` job ran Playwright successfully, and the scenario's passing result is present in the verified Allure artifact. |
| `failed` | Playwright ran and reported a failure for the correlated scenario. |
| `not_run` | The job or scenario was skipped, or the workflow ended before Playwright ran it. |
| `evidence_missing` | Playwright execution was reported, and required evidence is unavailable or mismatched. |
| `not_applicable` | QA confirmed the approved content at a pinned `main` revision. |

`check` covers Gherkin validation and has its own `gherkinCheckStatus`. The current E2E workflow requires `E2E_BASE_URL` for push and same-repository PR storefront runs. An orchestration status of `completed` means the run reached its final publication or CI observation. The browser outcome is shown separately. To avoid exposing service credentials, link to the GitHub artifact page and let GitHub handle access.

## Access and error handling

FastAPI validates a bearer access token and checks the caller's grants for starting, reading, and reviewing runs. Reviewer identity comes from the verified token issuer and subject. A caller must have run access and reviewer permission to decide. The API keeps BA, QA, GitHub read, and LangSmith credentials on the server. The browser uses stable problem codes to refresh a stale review and shows the new version before a second decision.

## Restart verification

Run [`checkpoint_restart_smoke.py`](../../verification/checkpoint_restart_smoke.py) against MongoDB 8.0 in the local QA-style environment or the PR workflow. Use one UUID for both invocations. The first process exits after writing an interrupt. The second checks the persisted hash, resumes the same thread, and checks the completed decision.

```sh
export AO_MONGODB_URI=mongodb://127.0.0.1:27017
run_id=$(python -c 'from uuid import uuid4; print(uuid4())')
uv run verification/checkpoint_restart_smoke.py pause "$run_id"
uv run verification/checkpoint_restart_smoke.py resume "$run_id"
```

## Reviewed revisions

- [BA handoff router and upstream revision](https://github.com/MichalBoczula/ECommerceStore.BA/blob/07ece81e6251d2d4b814b49e5ac70cc73f5e1212/src/ecommerce_store_ba/api/runs.py)
- [QA authoring and publication service](https://github.com/MichalBoczula/ECommerceStore.QA/blob/17654f38eb0fab5943240d3dc771025c5131c6e2/src/ecommerce_store_qa/application/e2e_service.py) and [GitHub publisher](https://github.com/MichalBoczula/ECommerceStore.QA/blob/17654f38eb0fab5943240d3dc771025c5131c6e2/src/ecommerce_store_qa/infrastructure/github_e2e.py)
- [E2E workflow](https://github.com/MichalBoczula/ECommerceStore.E2E/blob/d3e0e24a47f15c7fb1394fde928563f169a950e9/.github/workflows/ci.yml) and [catalog scenario](https://github.com/MichalBoczula/ECommerceStore.E2E/blob/d3e0e24a47f15c7fb1394fde928563f169a950e9/features/catalog/browse-phone-details.feature)
- [Knowledge Assistant Web feature layers and Kiota](https://github.com/MichalBoczula/ECommerceStore.KnowledgeAssistant.Web/blob/3135bc96752963519780ca43493ab5ee7fefe2f9/docs/adr/0001-feature-layers-and-kiota.md)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts), and [MongoDBSaver](https://pypi.org/project/langgraph-checkpoint-mongodb/0.5.1/)
