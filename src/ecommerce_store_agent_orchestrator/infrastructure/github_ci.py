from pydantic import BaseModel, Field

from ecommerce_store_agent_orchestrator.application.runs import WorkflowStepError
from ecommerce_store_agent_orchestrator.domain.review import (
    AllureArtifact,
    CiEvidence,
    EvidenceId,
    GherkinCheckResult,
    GitSha,
    Publication,
    PullNumber,
)
from ecommerce_store_agent_orchestrator.infrastructure.github import (
    E2E_REPOSITORY,
    E2E_URL,
    GitHubClient,
    GitHubCommit,
    GitHubPull,
)
from ecommerce_store_agent_orchestrator.infrastructure.storefront_evidence import (
    IncompleteEvidenceError,
    StorefrontProof,
    read_storefront_proof,
)


class _PullLink(BaseModel):
    number: PullNumber
    head: GitHubCommit


class _Run(BaseModel):
    id: EvidenceId
    run_number: EvidenceId
    run_attempt: EvidenceId
    event: str
    status: str
    head_sha: GitSha
    head_branch: str | None
    display_title: str = ""
    pull_requests: list[_PullLink] = Field(default_factory=list)

    def matches(self, publication: Publication) -> bool:
        if self.event != "pull_request" or (self.head_sha, self.head_branch) != (
            publication.commit_sha, publication.branch
        ):
            return False
        if self.pull_requests:
            return any(
                (link.number, link.head.sha) == (publication.pull_request_number, publication.commit_sha)
                for link in self.pull_requests
            )
        return self.display_title == f"Storefront PR #{publication.pull_request_number} at {publication.commit_sha}"


class _Runs(BaseModel):
    workflow_runs: list[_Run]


class _Step(BaseModel):
    name: str
    conclusion: str | None = None
    started_at: str | None = None


class _Job(BaseModel):
    id: EvidenceId
    name: str
    status: str
    conclusion: str | None
    steps: list[_Step] | None = None


class _Jobs(BaseModel):
    jobs: list[_Job]


class _Artifact(BaseModel):
    id: EvidenceId
    name: str
    expired: bool


class _Artifacts(BaseModel):
    artifacts: list[_Artifact]


class _TestedCommit(GitHubCommit):
    parents: list[GitHubCommit]


class GitHubCiReader:
    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    def observe(self, publication: Publication, feature_hash: str) -> CiEvidence:
        pull = self._client.read(f"pulls/{publication.pull_request_number}", GitHubPull)
        if (
            pull.number != publication.pull_request_number
            or pull.head.repo.full_name.casefold() != E2E_REPOSITORY.casefold()
        ):
            raise WorkflowStepError("publication_conflict", "GitHub reported a different published pull request.")
        evidence = CiEvidence(status="pending", gherkin_check_status="pending", current_head_sha=pull.head.sha)
        runs = self._client.read(
            "actions/workflows/ci.yml/runs",
            _Runs,
            params={
                "event": "pull_request",
                "head_sha": publication.commit_sha,
                "branch": publication.branch or "",
                "per_page": 100,
            },
        )
        matching = [run for run in runs.workflow_runs if run.matches(publication)]
        if not matching:
            return evidence

        run = max(matching, key=lambda item: (item.run_number, item.run_attempt))
        jobs = self._client.read(
            f"actions/runs/{run.id}/attempts/{run.run_attempt}/jobs", _Jobs, params={"per_page": 100}
        )
        check = next((job for job in jobs.jobs if job.name == "check"), None)
        storefront = next((job for job in jobs.jobs if job.name == "storefront"), None)
        evidence = CiEvidence(
            status="pending",
            gherkin_check_status=_gherkin_result(check, run.status),
            workflow_run_id=run.id,
            workflow_run_url=f"{E2E_URL}/actions/runs/{run.id}",
            run_attempt=run.run_attempt,
            storefront_job_id=storefront.id if storefront else None,
            observed_head_sha=run.head_sha,
            current_head_sha=pull.head.sha,
        )
        if run.status != "completed":
            return evidence
        return self._finished(evidence, publication, run, storefront, feature_hash)

    def _finished(
        self, evidence: CiEvidence, publication: Publication, run: _Run, storefront: _Job | None, feature_hash: str
    ) -> CiEvidence:
        if storefront is None or storefront.conclusion == "skipped":
            return evidence.model_copy(update={"status": "not_run", "reason_code": "storefront_skipped"})
        step = next(
            (
                step for step in storefront.steps or ()
                if step.name in ("Run storefront Playwright", "Run npm test")
            ),
            None,
        )
        if step is not None and step.started_at and step.conclusion in ("cancelled", "timed_out"):
            return evidence.model_copy(update={"status": "evidence_missing", "reason_code": "playwright_interrupted"})
        if step is None or step.conclusion not in ("success", "failure"):
            return evidence.model_copy(update={"status": "not_run", "reason_code": "playwright_not_run"})

        try:
            proof, artifact = self._proof(run)
            manifest = proof.manifest
            if (
                manifest.pr_number != publication.pull_request_number
                or manifest.pr_head_sha != publication.commit_sha
                or manifest.feature_sha256 != feature_hash
                or manifest.workflow_run_id != run.id
                or manifest.run_attempt != run.run_attempt
                or manifest.playwright_outcome != step.conclusion
                or not self._matches_checkout(manifest.tested_commit_sha, publication.commit_sha)
            ):
                raise IncompleteEvidenceError("revision_evidence_mismatch")
            observed = evidence.model_copy(update={
                "tested_commit_sha": manifest.tested_commit_sha,
                "allure_artifact": artifact,
            })
            if proof.scenario_status == "passed" and step.conclusion == storefront.conclusion == "success":
                return observed.model_copy(update={"status": "passed"})
            if proof.scenario_status in ("failed", "broken") and step.conclusion == "failure":
                return observed.model_copy(update={"status": "failed", "reason_code": "playwright_failed"})
            if proof.scenario_status == "skipped":
                return observed.model_copy(update={"status": "not_run", "reason_code": "scenario_skipped"})
            raise IncompleteEvidenceError("scenario_outcome_mismatch")
        except IncompleteEvidenceError as error:
            return evidence.model_copy(update={"status": "evidence_missing", "reason_code": error.reason_code})

    def _proof(self, run: _Run) -> tuple[StorefrontProof, AllureArtifact]:
        name = "storefront-test-results"
        if run.run_attempt > 1:
            name += f"-attempt-{run.run_attempt}"
        artifacts = self._client.read(
            f"actions/runs/{run.id}/artifacts", _Artifacts, params={"name": name, "per_page": 100}
        )
        matching = [artifact for artifact in artifacts.artifacts if artifact.name == name and not artifact.expired]
        if len(matching) != 1:
            raise IncompleteEvidenceError("allure_artifact_missing")
        artifact = matching[0]
        content = self._client.download_artifact(artifact.id)
        if content is None:
            raise IncompleteEvidenceError("allure_artifact_missing")
        result = AllureArtifact(
            id=artifact.id, name=name, url=f"{E2E_URL}/actions/runs/{run.id}/artifacts/{artifact.id}"
        )
        return read_storefront_proof(content), result

    def _matches_checkout(self, checkout: str, approved_head: str) -> bool:
        if checkout == approved_head:
            return True
        try:
            commit = self._client.read(
                f"commits/{checkout}",
                _TestedCommit,
                missing=WorkflowStepError("publication_revision_mismatch", "The tested commit cannot be resolved."),
            )
        except WorkflowStepError as error:
            if error.failure.code == "publication_revision_mismatch":
                return False
            raise
        return commit.sha == checkout and len(commit.parents) == 2 and commit.parents[1].sha == approved_head


def _gherkin_result(check: _Job | None, workflow_status: str) -> GherkinCheckResult:
    if check is None:
        return "not_run" if workflow_status == "completed" else "pending"
    if check.status != "completed":
        return "pending"
    if check.conclusion == "success":
        return "passed"
    if check.conclusion in ("failure", "timed_out"):
        return "failed"
    return "not_run"
