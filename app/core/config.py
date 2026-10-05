import json
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/salonbook"
    )
    REDIS_URL: str = "redis://localhost:6379/0"
    SECRET_KEY: str = "change-me"
    # Fail closed: anything other than an explicit local/test is treated as production.
    ENVIRONMENT: str = "production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    PORT: int = 8000
    PAYSTACK_SECRET_KEY: str = ""
    RESEND_API_KEY: str = ""

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        # Accept a JSON list or a plain comma-separated string.
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return value

    @field_validator("CORS_ORIGINS")
    @classmethod
    def reject_wildcard_origins(cls, value: list[str]) -> list[str]:
        for origin in value:
            if origin == "*" or not origin.startswith(("http://", "https://")):
                raise ValueError(
                    "CORS_ORIGINS must be explicit http(s) origins, never *"
                )
        return value

    @field_validator("DATABASE_URL")
    @classmethod
    def use_asyncpg_driver(cls, value: str) -> str:
        # Render hands out postgres:// or postgresql:// URLs; the async engine needs asyncpg.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix) :]
        return value

    @model_validator(mode="after")
    def reject_weak_secret_in_production(self) -> "Settings":
        if self.ENVIRONMENT.lower() not in {"local", "test"} and (
            self.SECRET_KEY == "change-me" or len(self.SECRET_KEY) < 32
        ):
            raise ValueError(
                "SECRET_KEY must be a random value of at least 32 characters in production"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
