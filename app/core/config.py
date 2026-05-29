"""Application configuration.

pydantic-settings is the single source of truth for every environment
variable.  No ``os.environ`` calls appear anywhere in the codebase.
All variables are documented in ``.env.example``.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── Core ─────────────────────────────────────────────────────────────────
    ENVIRONMENT: Literal["development", "test", "production"] = "development"
    SECRET_KEY: str
    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379"
    ALLOWED_ORIGINS: str = "http://localhost:3000"
    LOG_LEVEL: str = "info"

    # ── JWT ───────────────────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # ── Phone hashing ────────────────────────────────────────────────────────
    # Minimum 32 random bytes, base64-encoded.  Used to salt SHA-256 hashes of
    # phone numbers and analyst email addresses so that a raw rainbow table
    # cannot reverse stored hashes.
    PHONE_HASH_SALT: str = ""

    # ── SMS gateway ───────────────────────────────────────────────────────────
    SMS_GATEWAY: str = "console"  # console | africastalking | twilio
    AFRICASTALKING_API_KEY: str = ""
    AFRICASTALKING_USERNAME: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def allowed_origins_list(self) -> list[str]:
        """Split the comma-separated ALLOWED_ORIGINS string into a list."""
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


settings = Settings()  # type: ignore[call-arg]