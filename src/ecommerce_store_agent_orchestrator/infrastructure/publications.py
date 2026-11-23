from base64 import b64decode
from hashlib import sha256

from pydantic import BaseModel

from ecommerce_store_agent_orchestrator.application.runs import WorkflowStepError
from ecommerce_store_agent_orchestrator.domain.review import Publication, PublicationReceipt
from ecommerce_store_agent_orchestrator.infrastructure.github import (
    E2E_REPOSITORY,
    E2E_URL,
    GitHubClient,
    GitHubCommit,
    GitHubPull,
)


class _Ref(BaseModel):
    object: GitHubCommit


class _Feature(BaseModel):
    encoding: str
    content: str


class GitHubPublicationReader:
    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    def verify(self, receipt: PublicationReceipt, feature_path: str, feature_hash: str) -> Publication:
        if receipt.repository.casefold() != E2E_REPOSITORY.casefold():
            raise WorkflowStepError("publication_conflict", "QA reported a different E2E repository.")

        if receipt.status == "pull_request":
            result = self._pull(receipt)
        else:
            main = self._client.read("git/ref/heads/main", _Ref)
            commit = main.object.sha
            result = Publication(
                outcome="already_current",
                repository=E2E_REPOSITORY,
                url=f"{E2E_URL}/blob/{commit}/{feature_path}",
                commit_sha=commit,
            )

        feature = self._client.read(
            f"contents/{feature_path}",
            _Feature,
            params={"ref": result.commit_sha},
            missing=WorkflowStepError("publication_revision_mismatch", "The recorded commit has no feature file."),
        )
        try:
            content = b64decode("".join(feature.content.split()), validate=True)
        except ValueError as error:
            raise WorkflowStepError(
                "publication_revision_mismatch", "GitHub did not return the feature bytes at the recorded revision."
            ) from error
        if feature.encoding != "base64" or sha256(content).hexdigest() != feature_hash:
            raise WorkflowStepError(
                "publication_revision_mismatch", "The published revision does not contain the approved Gherkin."
            )
        return result

    def _pull(self, receipt: PublicationReceipt) -> Publication:
        pull = self._client.read(f"pulls/{receipt.number}", GitHubPull)
        if (
            pull.number != receipt.number
            or pull.head.ref != receipt.branch
            or pull.head.repo.full_name.casefold() != E2E_REPOSITORY.casefold()
            or pull.base.repo.full_name.casefold() != E2E_REPOSITORY.casefold()
            or pull.base.ref != "main"
        ):
            raise WorkflowStepError("publication_conflict", "GitHub reported a different PR target or branch.")
        return Publication(
            outcome="pull_request",
            repository=E2E_REPOSITORY,
            url=f"{E2E_URL}/pull/{pull.number}",
            commit_sha=pull.head.sha,
            pull_request_number=pull.number,
            branch=pull.head.ref,
        )
