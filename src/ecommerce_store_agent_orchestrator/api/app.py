from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

from ecommerce_store_agent_orchestrator.api.problems import register_problem_handlers
from ecommerce_store_agent_orchestrator.api.routes import router
from ecommerce_store_agent_orchestrator.application.runs import RunService
from ecommerce_store_agent_orchestrator.infrastructure.auth import AccessTokenVerifier
from ecommerce_store_agent_orchestrator.infrastructure.mongo import MongoRunRepository, create_client
from ecommerce_store_agent_orchestrator.infrastructure.settings import ApiSettings


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config = settings or ApiSettings()
        with create_client(config) as client:
            repository = MongoRunRepository(client[config.mongodb_database])
            await run_in_threadpool(repository.ensure_indexes)
            app.state.run_service = RunService(repository)
            app.state.token_verifier = AccessTokenVerifier(
                issuer=config.auth_issuer,
                audience=config.auth_audience,
                jwks_url=str(config.auth_jwks_url),
            )
            yield

    app = FastAPI(title="ECommerceStore Agent Orchestrator API", version="0.1.0", lifespan=lifespan)
    register_problem_handlers(app)
    app.include_router(router)
    return app
