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
    #
    # To use AWS Free Tier:
    #   1. Create a free account at https://aws.amazon.com/free/
    #   2. Set MODERATION_PROVIDER=rekognition
    #   3. Set AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY below.
    #
    # To avoid AWS entirely, keep MODERATION_PROVIDER=mock.
    MODERATION_PROVIDER: str = "mock"

    # ── Object storage (S3 / MinIO / Cloudflare R2) ───────────────────────────
    # STORAGE_BACKEND=mock — in-memory, no credentials needed (default, dev/CI).
    # STORAGE_BACKEND=s3   — AWS S3, MinIO, or any S3-compatible service.
    #
    # Free options:
    #   • AWS S3 Free Tier: 5 GB, 20 k GET + 2 k PUT/month for 12 months.
    #   • MinIO (self-hosted, completely free):
    #       docker run -p 9000:9000 quay.io/minio/minio server /data
    #     Then set:  S3_ENDPOINT_URL=http://localhost:9000
    #                S3_BUCKET_NAME=crisismap
    #                AWS_ACCESS_KEY_ID=minioadmin
    #                AWS_SECRET_ACCESS_KEY=minioadmin
    STORAGE_BACKEND: Literal["mock", "s3"] = "mock"

    # ── AWS credentials (used by Rekognition and/or S3) ───────────────────────
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None

    # ── S3 / MinIO bucket ─────────────────────────────────────────────────────
    S3_BUCKET_NAME: str | None = None
    # Leave empty for AWS S3.  MinIO / R2 example: http://localhost:9000
    S3_ENDPOINT_URL: str | None = None
    # Storage
    storage_backend: str = "mock"
    s3_endpoint_url: str | None = None
    s3_bucket_name: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_region: str = "us-east-1"

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
