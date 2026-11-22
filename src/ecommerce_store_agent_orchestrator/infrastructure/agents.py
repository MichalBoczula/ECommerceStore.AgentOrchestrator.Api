from uuid import UUID

from httpx2 import Client, RequestError, Response
from pydantic import BaseModel, ValidationError

from ecommerce_store_agent_orchestrator.application.agent_contracts import AuthorSession, PublishedHandoff, QaProposal
from ecommerce_store_agent_orchestrator.application.authoring import AuthoringError
from ecommerce_store_agent_orchestrator.domain.evidence import HandoffSnapshot
from ecommerce_store_agent_orchestrator.domain.runs import RunInput


class HttpAgents:
    def __init__(self, ba: Client, qa: Client) -> None:
        self._ba = ba
        self._qa = qa

    def read_handoff(self, request: RunInput) -> PublishedHandoff:
        path = f"api/v1/sessions/{request.ba_session_id}/runs/{request.ba_run_id}/handoff"
        try:
            response = self._ba.get(path)
        except RequestError as error:
            raise AuthoringError("integration_unavailable", "BA could not provide the published handoff.") from error
        if response.status_code == 404:
            raise AuthoringError("handoff_not_found", "BA could not find the requested handoff.")
        if response.status_code == 409:
            raise AuthoringError("handoff_not_published", "Publish the selected BA run before starting authoring.")
        if response.status_code != 200:
            raise AuthoringError("integration_unavailable", "BA could not provide the published handoff.")

        try:
            body = response.json()
            if isinstance(body, dict) and body.get("status") in ("draft", "prepared", "approved"):
                raise AuthoringError("handoff_not_published", "Publish the selected BA run before starting authoring.")
            return PublishedHandoff.model_validate(body)
        except ValueError as error:
            raise AuthoringError("handoff_version_mismatch", "BA returned an invalid published handoff.") from error

    def create_author_session(self, handoff: HandoffSnapshot) -> AuthorSession:
        response = self._send_qa(
            "POST",
            "api/v1/sessions",
            {"operation": "author", "jiraIssueKey": handoff.jira_issue_key, "snapshotId": handoff.snapshot_id},
        )
        return self._qa_result(response, 201, AuthorSession)

    def put_proposal(self, session_id: UUID, proposal_id: UUID, request: RunInput) -> QaProposal:
        response = self._send_qa(
            "PUT",
            f"api/v1/sessions/{session_id}/e2e-proposals/{proposal_id}",
            {
                "baSessionId": str(request.ba_session_id),
                "baRunId": str(request.ba_run_id),
                "criterionIndex": request.criterion_index,
            },
        )
        return self._qa_result(response, 200, QaProposal)

    def _send_qa(self, method: str, path: str, body: dict[str, str | int]) -> Response:
        try:
            return self._qa.request(method, path, json=body)
        except RequestError as error:
            raise AuthoringError("integration_unavailable", "QA authoring is temporarily unavailable.") from error

    @staticmethod
    def _qa_result[T: BaseModel](response: Response, status: int, model: type[T]) -> T:
        if response.status_code in (400, 404, 409, 413):
            raise AuthoringError("invalid_qa_proposal", "QA rejected the pinned handoff or authoring request.")
        if response.status_code != status:
            raise AuthoringError("integration_unavailable", "QA authoring is temporarily unavailable.")
        try:
            return model.model_validate_json(response.content)
        except ValidationError as error:
            raise AuthoringError("invalid_qa_proposal", "QA returned an invalid authoring response.") from error
