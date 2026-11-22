from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MongoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AO_")
    mongodb_uri: SecretStr = SecretStr("mongodb://localhost:27017")
    mongodb_database: str = "ecommerce_store_agent_orchestrator"


class ApiSettings(MongoSettings):
    auth_issuer: str = Field(min_length=1)
    auth_audience: str = Field(min_length=1)
    auth_jwks_url: AnyHttpUrl


class WorkerSettings(MongoSettings):
    worker_poll_seconds: float = Field(default=1, gt=0)
    worker_lease_seconds: int = Field(default=30, gt=0)
