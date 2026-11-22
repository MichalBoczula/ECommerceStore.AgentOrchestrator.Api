from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ecommerce_store_agent_orchestrator.api.contracts import (
    RunAccepted,
    RunDetail,
    RunPagePayload,
    RunSummary,
    StartRunBody,
)
from ecommerce_store_agent_orchestrator.api.problems import InsufficientScopeError, problem_responses
from ecommerce_store_agent_orchestrator.application.runs import RunService
from ecommerce_store_agent_orchestrator.domain.runs import RunOwner, RunStatus
from ecommerce_store_agent_orchestrator.infrastructure.auth import AccessTokenVerifier, InvalidAccessTokenError

_bearer = HTTPBearer(scheme_name="bearerAuth", bearerFormat="JWT", auto_error=False)


def _require_scope(scope: str) -> Callable[..., RunOwner]:
    def owner(
        request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
    ) -> RunOwner:
        if credentials is None:
            raise InvalidAccessTokenError
        verifier: AccessTokenVerifier = request.app.state.token_verifier
        principal = verifier.verify(credentials.credentials)
        if scope not in principal.scopes:
            raise InsufficientScopeError
        return principal.owner

    return owner


def _run_service(request: Request) -> RunService:
    return request.app.state.run_service


StartOwner = Annotated[RunOwner, Depends(_require_scope("runs.start"))]
ReadOwner = Annotated[RunOwner, Depends(_require_scope("runs.read"))]
Service = Annotated[RunService, Depends(_run_service)]

router = APIRouter(prefix="/api/v1/runs", tags=["Runs"])


@router.post(
    "",
    status_code=202,
    operation_id="startRun",
    responses={
        202: {"headers": {"Location": {"schema": {"type": "string", "format": "uri-reference"}}}},
        **problem_responses(400, 401, 403, 409, 503),
    },
)
def start_run(
    body: StartRunBody,
    response: Response,
    owner: StartOwner,
    service: Service,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9._:-]{1,128}$")],
) -> RunAccepted:
    run_id = service.start(owner, idempotency_key, body.to_domain())
    response.headers["Location"] = f"/api/v1/runs/{run_id}"
    return RunAccepted(run_id=run_id)


@router.get(
    "",
    operation_id="listRuns",
    response_model_exclude_none=True,
    responses=problem_responses(400, 401, 403, 503),
)
def list_runs(
    owner: ReadOwner,
    service: Service,
    status: RunStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
) -> RunPagePayload:
    page = service.list(owner, status, limit, cursor)
    return RunPagePayload(items=[RunSummary.model_validate(run) for run in page.items], next_cursor=page.next_cursor)


@router.get(
    "/{runId}",
    operation_id="getRun",
    response_model_exclude_none=True,
    responses=problem_responses(400, 401, 403, 404, 503),
)
def get_run(run_id: Annotated[UUID, Path(alias="runId")], owner: ReadOwner, service: Service) -> RunDetail:
    return RunDetail.model_validate(service.get(owner, run_id))
