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

    # ── Phone hashing ─────────────────────────────────────────────────────────
    # Minimum 32 random bytes, base64-encoded.  Used to salt SHA-256 hashes of
    # phone numbers and analyst email addresses so that a raw rainbow table
    # cannot reverse stored hashes even if the database is compromised.
    PHONE_HASH_SALT: str = ""

    # ── SMS gateway ───────────────────────────────────────────────────────────
    SMS_GATEWAY: str = "console"  # console | africastalking
    AFRICASTALKING_API_KEY: str = ""
    AFRICASTALKING_USERNAME: str = ""

    # ── Content moderation ────────────────────────────────────────────────────
    # MODERATION_PROVIDER=mock        — no AWS required (default, dev/CI).
    # MODERATION_PROVIDER=rekognition — AWS Rekognition; Free Tier: 5k images/month.
    MODERATION_PROVIDER: str = "mock"

    # ── Object storage (S3 / MinIO / Cloudflare R2) ───────────────────────────
    # STORAGE_BACKEND=mock — in-memory, no credentials needed (default, dev/CI).
    # STORAGE_BACKEND=s3   — AWS S3, MinIO, or any S3-compatible service.
    STORAGE_BACKEND: Literal["mock", "s3"] = "mock"

    # ── AWS credentials (used by Rekognition and/or S3) ───────────────────────
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None

    # ── S3 / MinIO bucket ─────────────────────────────────────────────────────
    S3_BUCKET_NAME: str | None = None
    # Leave empty for AWS S3.  MinIO / R2 example: http://localhost:9000
    S3_ENDPOINT_URL: str | None = None

    # ── GIS worker ────────────────────────────────────────────────────────────
    # Geocoding provider for landmark-based building matching (spec §9.2 step 3).
    # nominatim — free OpenStreetMap Nominatim API (default, no key required).
    # google    — Google Maps Geocoding API (requires GOOGLE_GEOCODING_API_KEY).
    # mock      — deterministic stub for tests and CI.
    GEOCODING_PROVIDER: str = "nominatim"

    # Required only when GEOCODING_PROVIDER=google.
    GOOGLE_GEOCODING_API_KEY: str = ""

    # Nearest-neighbour building search radius in metres (spec §9.2).
    # Expanded dynamically to min(accuracy_m * 1.5, 100) when GPS accuracy > 50 m.
    BUILDING_FOOTPRINT_SEARCH_RADIUS_M: int = 30

    # ── Celery ────────────────────────────────────────────────────────────────
    # Both default to REDIS_URL when left empty, so no change is needed for
    # development.  Override in production to use separate Redis databases or
    # a dedicated broker such as RabbitMQ.
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

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