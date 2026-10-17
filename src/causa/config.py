"""Runtime configuration, read from ``CAUSA_*`` environment variables or ``.env``."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Backend(StrEnum):
    EXASOL = "exasol"
    DUCKDB = "duckdb"  # local development / CI only; SQL is still authored in the Exasol dialect


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
