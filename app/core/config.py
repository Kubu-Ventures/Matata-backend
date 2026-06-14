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
    METRICS_TOKEN: str = ""  # static bearer token for /metrics scrape jobs

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
    # Optional sender ID or shortcode registered with your mobile network.
    # Required for Kenya (Safaricom rejects unregistered alphanumeric senders).
    # Set to your AT-assigned shortcode (e.g. "20880") or registered alphanumeric ID.
    # Leave empty to use Africa's Talking's default shared sender.
    AFRICASTALKING_SENDER_ID: str = ""
    # Voice OTP fallback via Africa's Talking outbound call (TTS).
    # Enable when SMS is carrier-rejected (e.g. Safaricom Kenya rejects unregistered).
    AFRICASTALKING_VOICE_ENABLED: bool = False
    # AT-assigned virtual phone number for outbound voice calls (e.g. "+254711082XXX").
    # Required when AFRICASTALKING_VOICE_ENABLED=true.
    AFRICASTALKING_VOICE_NUMBER: str = ""
    # Publicly reachable base URL of this server — used to build AT voice callback URLs.
    # Must be accessible from the internet when AFRICASTALKING_VOICE_ENABLED=true.
    # Example: https://api.crisismap.matata.org
    APP_PUBLIC_URL: str = ""

    # ── Email delivery ────────────────────────────────────────────────────────
    # EMAIL_PROVIDER=console  — prints to stdout; no network call (default, dev/CI).
    # EMAIL_PROVIDER=smtp     — sends via any SMTP server (Mailpit locally,
    #                           Postal / any MTA in production).
    #
    # Dev default points at the Mailpit container (docker-compose service
    # "mailpit").  In production point at your self-hosted Postal instance.
    EMAIL_PROVIDER: str = "console"  # console | smtp

    # SMTP connection settings — only required when EMAIL_PROVIDER=smtp.
    SMTP_HOST: str = "mailpit"  # docker-compose service name in dev
    SMTP_PORT: int = 1025  # Mailpit SMTP port (dev); Postal default 25/587
    SMTP_USERNAME: str = ""  # leave empty for Mailpit (no auth needed)
    SMTP_PASSWORD: str = ""  # leave empty for Mailpit (no auth needed)
    SMTP_USE_TLS: bool = False  # set True when using Postal with TLS (port 587)
    SMTP_USE_STARTTLS: bool = False  # set True for STARTTLS (port 587 on many MTAs)

    # The envelope / display From address for all outgoing mail.
    NOTIFICATION_FROM_EMAIL: str = "noreply@crisismap.matata.org"

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

    # Optional ISO 3166-1 alpha-2 country code to bias geocoding results toward
    # the deployment country (e.g. "ke" for Kenya, "ug" for Uganda, "ng" for
    # Nigeria).  Leave empty to allow global geocoding with no country bias —
    # required for multi-country or global deployments.
    GEOCODING_COUNTRY_CODE: str = ""

    # Nearest-neighbour building search radius in metres (spec §9.2).
    # Expanded dynamically to min(accuracy_m * 1.5, 100) when GPS accuracy > 50 m.
    BUILDING_FOOTPRINT_SEARCH_RADIUS_M: int = 30

    # ── Celery ────────────────────────────────────────────────────────────────
    # Both default to REDIS_URL when left empty, so no change is needed for
    # development.  Override in production to use separate Redis databases or
    # a dedicated broker such as RabbitMQ.
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    # ── AI / Vision worker ─────────────────────────────────────────────────────
    # VISION_PROVIDER=mock        — deterministic stub (default, dev/CI).
    # VISION_PROVIDER=openai      — GPT-4o (requires OPENAI_API_KEY).
    # VISION_PROVIDER=anthropic   — Claude claude-opus-4-6 (requires ANTHROPIC_API_KEY).
    # VISION_PROVIDER=ollama      — local open-source model via Ollama (free, no key).
    #                               Requires Ollama running locally with a
    #                               vision-capable model (e.g. `ollama pull llava`).
    VISION_PROVIDER: str = "mock"
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    # Ollama — only required when VISION_PROVIDER=ollama.
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_VISION_MODEL: str = "llava"
    # Alert ops when the AI queue depth exceeds this value.
    AI_PROCESSING_QUEUE_ALERT_DEPTH: int = 500

    # ── Confidence-based analyst routing thresholds ────────────────────────────
    # Rationale: responsible AI guidelines require that predictions
    # below 60% confidence are treated as operationally unreliable and must
    # receive mandatory human review before influencing response decisions.
    #
    # AI_CONFIDENCE_CRITICAL_THRESHOLD (< value → critical priority)
    #   0.60: model is essentially choosing between three severity levels with
    #   less than 60% certainty — too uncertain for autonomous action.
    #
    # AI_CONFIDENCE_HIGH_PRIORITY_THRESHOLD (< value → high priority)
    #   0.80: moderate confidence; divergence or borderline quality triggers
    #   analyst flag even when confidence is in this band.
    #
    # AI_QUALITY_CRITICAL_THRESHOLD (< value → critical priority)
    #   0.30: image is unusable; any severity prediction is noise.
    AI_CONFIDENCE_CRITICAL_THRESHOLD: float = 0.60
    AI_CONFIDENCE_HIGH_PRIORITY_THRESHOLD: float = 0.80
    AI_QUALITY_CRITICAL_THRESHOLD: float = 0.30
    # Baseline divergence threshold used before any analyst feedback has
    # accumulated.  Once get_ai_accuracy() has enough data it writes a
    # calibrated value to Redis and the AI worker reads it from there instead.
    AI_DIVERGENCE_THRESHOLD_DEFAULT: float = 0.70
    # Minimum number of high-confidence (ai_confidence > 0.7) feedback entries
    # required before the auto-calibration loop is allowed to write a new
    # threshold to Redis.  Below this count the sample is statistically too
    # small and the existing (or default) threshold is preserved unchanged.
    AI_DIVERGENCE_MIN_SAMPLE: int = 30
    # AI worker logs a WARNING when it reads a threshold that has not been
    # refreshed within this many days — a sign that analyst throughput has
    # dropped and the calibration may have drifted.
    AI_DIVERGENCE_STALENESS_DAYS: int = 7

    # ── Duplicate detection ────────────────────────────────────────────────────
    # Candidate reports must fall within ±DUPLICATE_TIME_WINDOW_HOURS of the
    # incoming report's created_at to be considered potential duplicates.
    # Rationale: two reports of the same building separated by more than this
    # window represent distinct damage events (e.g. flood then earthquake),
    # not the same event reported twice.
    #
    # 72 h accommodates delayed offline submissions and slow cellular sync in
    # field conditions while preventing cross-event false positives.
    DUPLICATE_TIME_WINDOW_HOURS: int = 72

    # ── i18n / Translation ────────────────────────────────────────────────────
    # TRANSLATION_PROVIDER=libretranslate  — calls a self-hosted LibreTranslate
    #                                        container (default; no API key needed
    #                                        for self-hosted).
    # TRANSLATION_PROVIDER=argostranslate  — offline open-source fallback;
    #                                        requires ``pip install argostranslate``
    #                                        and pre-installed language packages.
    # TRANSLATION_PROVIDER=mock            — deterministic stub (tests / CI only).
    TRANSLATION_PROVIDER: str = "libretranslate"

    # Base URL of the LibreTranslate service.  The Docker Compose service name
    # ``libretranslate`` resolves within the compose network automatically.
    # Override to ``https://libretranslate.com`` to use the public hosted API
    # (requires LIBRETRANSLATE_API_KEY).
    LIBRETRANSLATE_URL: str = "http://libretranslate:5000"

    # API key for the LibreTranslate *public hosted* API at libretranslate.com.
    # Leave empty (the default) for self-hosted instances — no key is required.
    LIBRETRANSLATE_API_KEY: str = ""

    # Default / fallback language code when Accept-Language negotiation yields
    # no supported match.
    DEFAULT_LANGUAGE: str = "en"

    # ── Dashboard ─────────────────────────────────────────────────────────────
    DASHBOARD_BASE_URL: str = "https://crisismap.matata.org"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env is shared with other services; skip unknown vars
    )

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def allowed_origins_list(self) -> list[str]:
        """Split the comma-separated ALLOWED_ORIGINS string into a list."""
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


settings = Settings()  # type: ignore[call-arg]
