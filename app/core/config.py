import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MIN_CRON_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        # A failed validation must not echo the raw settings (keys, DB password) into logs.
        hide_input_in_errors=True,
    )

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
    # Salon local time as a fixed UTC offset (default WAT, UTC+1, no DST). Availability
    # rules are stored as local wall-clock times; bookings are stored in UTC.
    SALON_UTC_OFFSET_MINUTES: int = Field(default=60, ge=-720, le=840)
    PAYSTACK_SECRET_KEY: str = ""
    RESEND_API_KEY: str = ""
    # Transactional email (Brevo). All optional: with the key or the sender address
    # missing, booking emails are skipped with a log line and the app runs as normal.
    # SecretStr: its repr and any settings dump show ********, never the key.
    BREVO_API_KEY: SecretStr = SecretStr("")
    EMAIL_FROM_ADDRESS: str = ""
    EMAIL_FROM_NAME: str = "SalonBook"
    # Shared secret the external scheduler sends in X-Cron-Secret to POST
    # /api/v1/internal/reminders/run. Empty means the endpoint answers 503 (fails closed).
    CRON_SECRET: SecretStr = SecretStr("")

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

    def _is_production(self) -> bool:
        # Fail closed: anything other than an explicit local/test is production.
        return self.ENVIRONMENT.lower() not in {"local", "test"}

    @model_validator(mode="after")
    def reject_weak_secret_in_production(self) -> "Settings":
        if self._is_production() and (
            self.SECRET_KEY == "change-me" or len(self.SECRET_KEY) < 32
        ):
            raise ValueError(
                "SECRET_KEY must be a random value of at least 32 characters in production"
            )
        return self

    @model_validator(mode="after")
    def reject_short_cron_secret(self) -> "Settings":
        # Empty (or blank) is allowed: the scheduler endpoint is then off. A secret that
        # is set must be long enough to resist guessing. Only the length is reported.
        secret = self.CRON_SECRET.get_secret_value().strip()
        if secret and len(secret) < MIN_CRON_SECRET_LENGTH:
            raise ValueError(
                f"CRON_SECRET is set but only {len(secret)} characters long; it must be "
                f"at least {MIN_CRON_SECRET_LENGTH} (or leave it empty to turn the "
                "scheduler endpoint off)"
            )
        return self

    @model_validator(mode="after")
    def require_paystack_key_in_production(self) -> "Settings":
        # Without it every webhook is rejected and every deposit fails: better to
        # refuse to boot than to find out at the first payment.
        if self._is_production() and not self.PAYSTACK_SECRET_KEY.strip():
            raise ValueError("PAYSTACK_SECRET_KEY must be set in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
