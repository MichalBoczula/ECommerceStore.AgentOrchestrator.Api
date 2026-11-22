from base64 import b64decode
from hashlib import sha256

from httpx2 import Client, RequestError
from pydantic import BaseModel, ValidationError

from ecommerce_store_agent_orchestrator.application.runs import WorkflowStepError
from ecommerce_store_agent_orchestrator.domain.review import GitSha, Publication, PublicationReceipt, PullNumber

_REPOSITORY = "MichalBoczula/ECommerceStore.E2E"
_REPO_API = f"repos/{_REPOSITORY}"
_REPO_URL = f"https://github.com/{_REPOSITORY}"


class _Repository(BaseModel):
    full_name: str


class _Branch(BaseModel):
    ref: str
    sha: GitSha
    repo: _Repository


class _Pull(BaseModel):
    number: PullNumber
    head: _Branch
    base: _Branch


class _Commit(BaseModel):
    sha: GitSha


class _Ref(BaseModel):
    object: _Commit


class _Feature(BaseModel):
    encoding: str
    content: str


class GitHubPublicationReader:
    def __init__(self, client: Client) -> None:
        self._client = client

    def verify(self, receipt: PublicationReceipt, feature_path: str, feature_hash: str) -> Publication:
        if receipt.repository.casefold() != _REPOSITORY.casefold():
            raise WorkflowStepError("publication_conflict", "QA reported a different E2E repository.")

        if receipt.status == "pull_request":
            result = self._pull(receipt)
        else:
            main = self._read(f"{_REPO_API}/git/ref/heads/main", _Ref)
            commit = main.object.sha
            result = Publication(
                outcome="already_current",
                repository=_REPOSITORY,
                url=f"{_REPO_URL}/blob/{commit}/{feature_path}",
                commit_sha=commit,
            )

        feature = self._read(f"{_REPO_API}/contents/{feature_path}", _Feature, params={"ref": result.commit_sha})
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
        pull = self._read(f"{_REPO_API}/pulls/{receipt.number}", _Pull)
        if (
            pull.number != receipt.number
            or pull.head.ref != receipt.branch
            or pull.head.repo.full_name.casefold() != _REPOSITORY.casefold()
            or pull.base.repo.full_name.casefold() != _REPOSITORY.casefold()
            or pull.base.ref != "main"
        ):
            raise WorkflowStepError("publication_conflict", "GitHub reported a different PR target or branch.")
        return Publication(
            outcome="pull_request",
            repository=_REPOSITORY,
            url=f"{_REPO_URL}/pull/{pull.number}",
            commit_sha=pull.head.sha,
            pull_request_number=pull.number,
            branch=pull.head.ref,
        )

    def _read[T: BaseModel](self, path: str, model: type[T], *, params: dict[str, str] | None = None) -> T:
        try:
            response = self._client.get(path, params=params)
        except RequestError as error:
            raise WorkflowStepError("integration_unavailable", "GitHub verification is unavailable.") from error
        if response.status_code == 404 and params is not None:
            raise WorkflowStepError("publication_revision_mismatch", "The recorded commit has no feature file.")
        if response.status_code != 200:
            raise WorkflowStepError("integration_unavailable", "GitHub publication verification is unavailable.")
        try:
            return model.model_validate_json(response.content)
        except ValidationError as error:
            raise WorkflowStepError(
                "integration_unavailable", "GitHub returned an invalid publication verification response."
            ) from error
