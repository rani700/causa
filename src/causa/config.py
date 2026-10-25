"""Runtime configuration, read from ``CAUSA_*`` environment variables or ``.env``."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Backend(StrEnum):
    EXASOL = "exasol"
    DUCKDB = "duckdb"  # local development / CI only; SQL is still authored in the Exasol dialect


class AgentProvider(StrEnum):
    AUTO = "auto"  # Anthropic when its key is set, else OpenAI when its key is set
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CAUSA_", env_file=".env", extra="ignore")

    backend: Backend = Backend.DUCKDB
    duckdb_path: Path = Path("data/causa.duckdb")

    exasol_dsn: str = "localhost:8563"
    exasol_user: str = "sys"
    exasol_password: SecretStr = SecretStr("exasol")
    exasol_encryption: bool = True
    # The docker-db image ships a self-signed certificate; never enable this against a real cluster.
    exasol_tls_insecure: bool = False
    exasol_connect_timeout: int = 10  # seconds
    # A DuckDB copy of the same data to answer from while Exasol is unreachable (e.g. an idle SaaS
    # cluster that stopped itself). Unset: no fallback, failures surface as errors.
    fallback_duckdb_path: Path | None = None
    fallback_retry_seconds: int = 120

    slack_webhook_url: SecretStr | None = None

    # The investigator agent. Keys use the providers' own variable names, from the environment or .env.
    agent_provider: AgentProvider = AgentProvider.AUTO
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY"))
    openai_api_key: SecretStr | None = Field(default=None, validation_alias=AliasChoices("OPENAI_API_KEY"))
    openai_model: str = "gpt-5-nano"  # the cheapest OpenAI model with function calling


@lru_cache
def get_settings() -> Settings:
    return Settings()
