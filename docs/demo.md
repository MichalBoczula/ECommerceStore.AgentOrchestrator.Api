# Catalog review demonstration

## Rehearse with synthetic evidence

Run the API checks with Python 3.14 and MongoDB 8.0. Worker subprocesses share the test database and checkpoint collection. A local HTTP fixture represents the BA, QA, GitHub and LangSmith endpoints.

~~~sh
docker compose up -d --wait mongo
uv sync --locked
uv run pytest -q tests/unit
uv run pytest -q -s tests/integration/test_runs.py -k 'fresh_worker_resumes or publication_recovers_one_pull or rejection_resumes or failed_handoff or invalid_proposal'
~~~

| Rehearsal | Result to verify |
| --- | --- |
| Resume after worker exit | The next worker reads the MongoDB checkpoint and advances the original run. |
| Recover an approved publication | QA creates one fixture PR before dropping the response. A new worker resumes publication and the worker later verifies the exact PR revision, workflow attempt and Allure result. The command prints the IDs and statuses for this rehearsal. |
| Reject an approved-version review | The stored rejection resumes the checkpoint and ends the run at review. The fixture records zero PRs. |
| Evaluate mismatched evidence | A different snapshot, changed criterion or mismatched feature hash ends the run before review or publication. |
| Compare worker traces | Successive worker processes send the same pseudonymous correlation ID. The fixture records stage, attempt, status and timestamps. |

From the [Web repository](https://github.com/MichalBoczula/ECommerceStore.AgentOrchestrator.Web), run the browser rehearsal on the same contract.

~~~sh
npm ci
npx playwright install chromium
npm run test:e2e -- e2e/ba-to-allure.spec.ts
~~~

The browser rehearsal uses synthetic API responses to walk from the run list through review to the PR, workflow, and Allure links. The Docker-backed API checks exercise the real checkpoint and verification code against fixture responses.

## Walk through the configured services

1. Use a published BA handoff for the Products Catalog journey. Pin its session, run, draft SHA-256 and the zero-based catalog-to-details criterion in `AO_SUPPORTED_HANDOFF`. Supply the same session, run and criterion in `POST /api/v1/runs`. Use a new idempotency key for each separate demonstration. [API setup](../README.md#enable-ba-and-qa-authoring)
2. Use an API account with `runs.start`, `runs.read`, and `runs.review`. Start the API and worker with the same MongoDB settings and point the Web panel at the API. Set `AO_LANGSMITH_API_KEY` on the worker if you want traces in your LangSmith project. [Web setup](https://github.com/MichalBoczula/ECommerceStore.AgentOrchestrator.Web#run-locally)
3. Warm the Azure storefront before the demo and use the configured E2E repository's `E2E_BASE_URL`. Have QA prepare a change to the managed feature if you want the PR path. Identical Gherkin on `main` yields a pinned feature permalink. [Warm-up workflow](https://github.com/MichalBoczula/ECommerceStore.Terraform/actions/workflows/start-dev-apps.yml) · [Storefront E2E](https://github.com/MichalBoczula/ECommerceStore.E2E/actions/workflows/ci.yml)
4. Open `/runs/<runId>`. At `awaiting_review`, compare the BA source and snapshot with the Gherkin. Stop and restart the worker using the same database. Approve the displayed hash on the restarted run. For the rejection path, start a separate run and record a reason.
5. Refresh the approved run until the publication and CI evidence appear. For an intentional lost publication response, use the local rehearsal above. It exercises resume and duplicate prevention with a deterministic fixture.

| Evidence | Where to find it |
| --- | --- |
| BA draft and criterion | The handoff, snapshot, source revision and criterion in run details. |
| Reviewed Gherkin revision | The feature SHA-256 next to the Gherkin and the recorded review decision. |
| Published and tested revisions | The PR link and published, observed, latest and tested commit fields when available. |
| Browser result | The Playwright status, Actions workflow and attempt, and the matching Allure artifact. Gherkin validation appears under its own status. |
| LangSmith stages | Use the trace correlation ID in the timeline to find the worker attempts by the `correlation_id` metadata in the configured project. |
