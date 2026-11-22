from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from ecommerce_store_agent_orchestrator.application.runs import (
    IdempotencyConflictError,
    InvalidCursorError,
    RunNotFoundError,
)
from ecommerce_store_agent_orchestrator.infrastructure.auth import (
    IdentityProviderUnavailableError,
    InvalidAccessTokenError,
)


class InsufficientScopeError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Problem:
    status: int
    code: str
    title: str


_PROBLEMS = {
    RequestValidationError: _Problem(400, "invalid_request", "Invalid request"),
    InvalidCursorError: _Problem(400, "invalid_request", "Invalid pagination cursor"),
    InvalidAccessTokenError: _Problem(401, "unauthorized", "A valid access token is required"),
    InsufficientScopeError: _Problem(403, "forbidden", "The caller lacks permission for this operation"),
    RunNotFoundError: _Problem(404, "run_not_found", "Run not found"),
    IdempotencyConflictError: _Problem(409, "idempotency_key_reused", "Idempotency key already used"),
    IdentityProviderUnavailableError: _Problem(503, "orchestrator_unavailable", "Identity provider unavailable"),
    PyMongoError: _Problem(503, "orchestrator_unavailable", "Run storage unavailable"),
}


async def _problem_response(request: Request, exc: Exception) -> JSONResponse:
    problem = next(problem for error, problem in _PROBLEMS.items() if isinstance(exc, error))
    headers = {"WWW-Authenticate": "Bearer"} if problem.status == 401 else None
    return JSONResponse(
        status_code=problem.status,
        media_type="application/problem+json",
        headers=headers,
        content={"title": problem.title, "status": problem.status, "code": problem.code, "instance": request.url.path},
    )


def register_problem_handlers(app: FastAPI) -> None:
    for error in _PROBLEMS:
        app.add_exception_handler(error, _problem_response)


def problem_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    responses: dict[int | str, dict[str, Any]] = {
        status: {
            "description": next(problem.title for problem in _PROBLEMS.values() if problem.status == status),
            "content": {"application/problem+json": {"schema": {"type": "object"}}},
        }
        for status in status_codes
    }
    responses["default"] = {"description": "Unexpected response."}
    return responses
