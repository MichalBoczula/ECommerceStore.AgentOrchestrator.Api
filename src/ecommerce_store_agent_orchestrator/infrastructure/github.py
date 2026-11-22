from urllib.parse import urlsplit

from httpx2 import Client, RequestError, Response
from pydantic import BaseModel, ValidationError

from ecommerce_store_agent_orchestrator.application.runs import WorkflowStepError
from ecommerce_store_agent_orchestrator.domain.review import GitSha, PullNumber

E2E_REPOSITORY = "MichalBoczula/ECommerceStore.E2E"
E2E_URL = f"https://github.com/{E2E_REPOSITORY}"
_REPO_API = f"repos/{E2E_REPOSITORY}"
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


class _Repository(BaseModel):
    full_name: str


class _Branch(BaseModel):
    ref: str
    sha: GitSha
    repo: _Repository


class GitHubPull(BaseModel):
    number: PullNumber
    head: _Branch
    base: _Branch


class GitHubCommit(BaseModel):
    sha: GitSha


class GitHubClient:
    def __init__(self, client: Client) -> None:
        self._client = client

    def read[T: BaseModel](
        self,
        path: str,
        model: type[T],
        *,
        params: dict[str, str | int] | None = None,
        missing: WorkflowStepError | None = None,
    ) -> T:
        try:
            response = self._client.get(f"{_REPO_API}/{path}", params=params)
        except RequestError as error:
            raise WorkflowStepError("integration_unavailable", "GitHub verification is unavailable.") from error
        if response.status_code == 404 and missing is not None:
            raise missing
        if response.status_code != 200:
            raise WorkflowStepError("integration_unavailable", "GitHub verification is unavailable.")
        try:
            return model.model_validate_json(response.content)
        except ValidationError as error:
            raise WorkflowStepError("integration_unavailable", "GitHub returned an invalid response.") from error

    def download_artifact(self, artifact_id: int) -> bytes | None:
        path = f"{_REPO_API}/actions/artifacts/{artifact_id}/zip"
        try:
            with self._client.stream("GET", path, follow_redirects=False) as response:
                if not response.is_redirect:
                    return _read_archive(response)
                redirect = response.headers.get("Location", "")
            location = urlsplit(redirect)
            if location.scheme != "https" or not location.hostname or location.username is not None:
                return None
            # The signed download is served by storage and does not need the GitHub bearer token.
            with Client(timeout=10) as download, download.stream("GET", redirect) as archive:
                return _read_archive(archive)
        except RequestError, ValueError:
            raise WorkflowStepError("integration_unavailable", "GitHub artifact download is unavailable.") from None


def _read_archive(response: Response) -> bytes | None:
    if response.status_code in (401, 403, 404, 410):
        return None
    if response.status_code != 200:
        raise WorkflowStepError("integration_unavailable", "GitHub artifact download is unavailable.")
    content = bytearray()
    for chunk in response.iter_bytes():
        if len(content) + len(chunk) > _MAX_ARTIFACT_BYTES:
            return None
        content.extend(chunk)
    return bytes(content)
