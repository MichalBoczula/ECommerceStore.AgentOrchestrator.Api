from typing import Self

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ecommerce_store_agent_orchestrator.application.agent_contracts import HandoffPin


class MongoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AO_", hide_input_in_errors=True)
    mongodb_uri: SecretStr = SecretStr("mongodb://localhost:27017")
    mongodb_database: str = "ecommerce_store_agent_orchestrator"


class ApiSettings(MongoSettings):
    auth_issuer: str = Field(min_length=1)
    auth_audience: str = Field(min_length=1)
    auth_jwks_url: AnyHttpUrl


class WorkerSettings(MongoSettings):
    worker_poll_seconds: float = Field(default=1, gt=0)
    worker_lease_seconds: int = Field(default=30, gt=0)
    ba_api_url: AnyHttpUrl | None = None
    ba_api_key: SecretStr | None = None
    qa_api_url: AnyHttpUrl | None = None
    qa_api_key: SecretStr | None = None
    supported_handoff: HandoffPin | None = None

    @model_validator(mode="after")
    def validate_authoring_configuration(self) -> Self:
        values = (self.ba_api_url, self.ba_api_key, self.qa_api_url, self.qa_api_key, self.supported_handoff)
        if any(value is not None for value in values) and not all(values):
            raise ValueError("Configure both agent API URLs, both API keys, and the supported BA handoff together.")
        return self
