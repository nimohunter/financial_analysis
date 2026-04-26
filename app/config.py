from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = Field(default="")
    sec_user_agent_email: str = Field(default="")
    database_url: str = Field(default="postgresql://finagent:password@postgres:5432/finagent")
    readonly_database_url: str = Field(default="postgresql://readonly:password@postgres:5432/finagent")


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def load_yaml() -> dict[str, Any]:
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)
