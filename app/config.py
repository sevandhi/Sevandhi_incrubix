"""All runtime settings, read from the environment (and `.env`) with pydantic-settings.

Nothing else in the code reads ``os.environ``; every path, URL, key and limit lives here, and every
setting is listed in ``.env.example``. Secret values are ``SecretStr`` so they never print by accident.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Settings that may be copied into job manifests (IS9). Anything not listed here is never exported,
# so a new secret setting can not leak by default (red-team #6: allow-list, not block-list).
SAFE_SNAPSHOT_KEYS: tuple[str, ...] = (
    "APP_ENV",
    "SERVICE_VERSION",
    "GIT_SHA",
    "SIMULATION_MODE",
    "DISABLED_CONNECTORS",
    "TEST_POST_TTL_S",
    "CIRCUIT_FAILS",
    "CIRCUIT_OPEN_S",
    "IDEMPOTENCY_TTL_H",
    "IDEMPOTENCY_LOCK_WAIT_S",
    "IDEMPOTENCY_SETTLE_S",
    "PLATFORM_TIMEOUT_S",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "RATE_LIMIT_PER_MIN",
    "S3_BUCKET",
    "S3_PRESIGN_EXPIRY_S",
    "JOB_MAX_ATTEMPTS",
    "SEED",
    "PUBLIC_BASE_URL",
    "FILE_LINK_EXPIRY_S",
    "MEDIA_MAX_BYTES",
    "CERT_OWN_HOSTS",
    "FETCH_LOCAL_HOSTS",
    "SWEEPER_EVERY_S",
    "METRICS_POLL_EVERY_S",
    "RECERT_AFTER_DAYS",
    "RECERT_TICK_EVERY_S",
    "RECERT_CONCURRENCY",
    "RECERT_JITTER_S",
    "RULES_DRIFT_ENABLED",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=True)

    # --- service ---
    APP_ENV: Literal["dev", "test", "demo", "prod"] = "dev"
    SERVICE_VERSION: str = "0.1.0"
    GIT_SHA: str = "unknown"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True
    SEED: int = 42

    # --- database ---
    DATABASE_URL: str = "postgresql+asyncpg://ibx:ibx-dev-password@postgres:5432/ibx"
    DB_AUTO_MIGRATE: bool = True
    DB_POOL_SIZE: int = 10

    # --- object storage (SeaweedFS S3 gateway) ---
    S3_ENDPOINT: str = "http://s3:8333"
    S3_PUBLIC_ENDPOINT: str = "http://127.0.0.1:8333"
    S3_ACCESS_KEY: SecretStr = SecretStr("ibx-dev-access")
    S3_SECRET_KEY: SecretStr = SecretStr("ibx-dev-secret")
    S3_BUCKET: str = "ibx"
    S3_REGION: str = "us-east-1"
    S3_PRESIGN_EXPIRY_S: int = 3600

    # --- API security ---
    API_TOKEN: SecretStr = SecretStr("ibx-dev-token")
    API_TOKEN_REQUIRED: bool = True  # False = open demo: no bearer token needed (the per-IP rate limit still applies)
    SECRETS_KEY: SecretStr | None = None  # Fernet key; derived from API_TOKEN in dev when empty
    CALLBACK_SECRET: SecretStr | None = None  # HMAC key for job callbacks; defaults to API_TOKEN
    RATE_LIMIT_PER_MIN: int = 600  # per client IP; 0 disables
    TRUSTED_PROXIES: Annotated[list[str], NoDecode] = Field(default_factory=list)  # IPs/CIDRs whose X-Forwarded-For is believed
    HTTP_IDEMPOTENCY_ENABLED: bool = True

    # --- simulation / mocks ---
    SIMULATION_MODE: bool = False  # true: every connection is rewritten to mock-platforms (zero keys)
    MOCK_PLATFORM_URL: str = "http://mock-platforms:8080"
    SIM_SECRET: str = "sim-secret"  # placeholder signing secret our partner mock accepts (simulation only, not a real secret)
    BOOTSTRAP_OUT_DIR: str = "/bootstrap/out"  # credentials written by deploy/bootstrap (lemmy.json, ghost.json)
    IBX_TOXIPROXY_URL: str = "http://toxiproxy:8474"  # toxiproxy control API (fault injection on real instances)
    IBX_LEMMY_PROXY_URL: str = "http://toxiproxy:18536"
    IBX_GHOST_PROXY_URL: str = "http://toxiproxy:12368"
    # all-in-one live demo (app.demo.allinone, Dockerfile.demo, render.yaml): seed + certify the demo destinations at start
    DEMO_SEED_ON_START: bool = True

    # --- connectors and reliability ---
    DISABLED_CONNECTORS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    PLATFORM_TIMEOUT_S: float = 10.0
    TEST_POST_TTL_S: int = 300
    CIRCUIT_FAILS: int = 5
    CIRCUIT_OPEN_S: int = 300
    IDEMPOTENCY_TTL_H: int = 24
    IDEMPOTENCY_LOCK_WAIT_S: float = 15.0
    IDEMPOTENCY_SETTLE_S: float = 5.0
    RATE_LIMIT_MAX_WAIT_S: float = 2.0  # how long the local token bucket may wait before failing fast
    RATE_LIMIT_DEFAULT_RETRY_S: int = 60  # used when a platform says "rate limited" without a reset time
    LEMMY_RL_FALLBACK_S: int = 60

    # --- jobs ---
    WORKER_POLL_S: float = 2.0
    WORKER_CONCURRENCY: int = 4
    JOB_MAX_ATTEMPTS: int = 5
    JOB_STALE_AFTER_S: int = 900
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_TICK_S: float = 5.0
    CALLBACK_TIMEOUT_S: float = 5.0
    WORKER_HEARTBEAT_FILE: str = "/tmp/ibx-worker-heartbeat"  # noqa: S108 - container-local liveness file (compose healthcheck)

    # --- LLM (free only). auto = gemini -> groq -> openrouter -> ollama -> mock; openai_compat = local Ollama.
    # Provider keys and models live in app/providers/llm/config.py. ---
    LLM_PROVIDER: Literal["auto", "gemini", "groq", "openrouter", "ollama", "mock", "openai_compat"] = "mock"
    OLLAMA_URL: str = "http://ollama:11434/v1"
    LLM_MODEL: str = "mock-heuristic-v1"
    LLM_PRICE_PER_1K_TOKENS_USD: float = 0.0  # local models are free; used for the per-unit cost in manifests

    # --- files in/out (IS7), certification and the test-post sweeper ---
    PUBLIC_BASE_URL: str = "http://127.0.0.1:8088"  # how callers reach the API; signed download links start here
    FILE_LINK_EXPIRY_S: int = 3600  # lifetime of signed /api/v1/files links
    MEDIA_MAX_BYTES: int = 52_428_800  # 50 MiB per uploaded media file
    MEDIA_ALLOWED_MIME: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["image/png", "image/jpeg", "image/gif", "image/webp", "video/mp4", "video/webm", "application/pdf"]
    )
    MEDIA_FETCH_TIMEOUT_S: float = 15.0
    CERT_OWN_HOSTS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["lemmy", "ghost", "discourse", "mock-platforms", "toxiproxy", "localhost", "127.0.0.1"]
    )  # certification test posts may only go to these hosts (or to mock-platforms in simulation)
    FETCH_LOCAL_HOSTS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["mock-platforms", "lemmy", "ghost", "discourse", "localhost", "127.0.0.1"]
    )  # our own containers that URL fetches may reach, only in simulation or dev/test (app.reliability.safe_fetch)
    CERT_LIVE_TIMEOUT_S: float = 30.0  # how long CONFIRM_LIVE / CONFIRM_DELETED poll the live URL
    SWEEPER_EVERY_S: int = 60  # the test-post TTL sweeper's period
    CERT_RESCHEDULE_MAX: int = 3  # a rate-limited / circuit-open run is rescheduled (level kept) at most this many times in a row
    CERT_RESCHEDULE_MAX_DELAY_S: int = 3600  # never wait longer than this for a platform's reset time
    # Evidence screenshots (compose profile `evidence`, deploy/shooter): unset = no screenshots, recorded as such.
    SCREENSHOT_URL: str | None = None  # e.g. http://shooter:3000
    SCREENSHOT_URL_REWRITES: Annotated[list[str], NoDecode] = Field(default_factory=list)  # "from=>to" prefixes, e.g. a Lemmy post via lemmy-ui

    # --- metrics, recertification, rules drift, alerts (app/metrics, app/recert, app/providers/alerts) ---
    METRICS_POLL_EVERY_S: int = 21600  # scheduled metrics poll of recent publications (6 h)
    METRICS_POLL_WINDOW_DAYS: int = 30  # only publications published in this window are polled
    METRICS_POLL_MAX: int = 200  # publications per poll run
    RECERT_AFTER_DAYS: int = Field(default=21, ge=1, le=29)  # re-test L1+ destinations certified longer ago (< 30 days)
    RECERT_TICK_EVERY_S: int = 86400  # the recertification tick runs daily
    RECERT_MAX_PER_TICK: int = 100  # destinations enqueued per tick (the rest wait for the next tick)
    RECERT_CONCURRENCY: int = 2  # recertification runs at the same time, across all workers
    RECERT_JITTER_S: int = 600  # each run starts 0..JITTER s after the tick (stable per destination and day)
    RECERT_RETRY_EXCLUDED_AFTER_S: int = 86400  # a destination demoted by certification is re-tested after this
    RECERT_SELF_HEAL_EVERY_S: int = 60  # scan circuit_state for open breakers / schema mismatches
    RECERT_SELF_HEAL_COOLDOWN_S: int = 3600  # at most one self-heal run per destination in this window
    RULES_DRIFT_ENABLED: bool = True  # recertification re-hashes the rules sources and flags changes
    ALERT_DELIVER_EVERY_S: int = 60  # deliver new / resolved alerts to the configured sinks
    ALERT_WEBHOOK_URL: str | None = None  # HMAC-signed JSON POST per alert event; empty = off
    ALERT_WEBHOOK_SECRET: SecretStr | None = None  # HMAC key for the alert webhook; defaults to CALLBACK_SECRET/API_TOKEN
    GITHUB_TOKEN: SecretStr | None = None  # optional: open (and close) a GitHub issue per alert
    GITHUB_REPO: str | None = None  # "owner/repo" for the alert issues
    GITHUB_API_URL: str = "https://api.github.com"

    @field_validator("DISABLED_CONNECTORS", "MEDIA_ALLOWED_MIME", "CERT_OWN_HOSTS", "FETCH_LOCAL_HOSTS", "TRUSTED_PROXIES",
                     "SCREENSHOT_URL_REWRITES", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return value

    # ------------------------------------------------------------------ helpers

    @property
    def sim_tools_enabled(self) -> bool:
        """Fault injection and mock counters: on in simulation, dev, test and demo; never in prod."""
        return self.SIMULATION_MODE or self.APP_ENV in ("dev", "test", "demo")

    def fernet_key(self) -> bytes:
        """The Fernet key for secrets at rest. In dev it is derived from API_TOKEN so api and worker agree."""
        if self.SECRETS_KEY is not None and self.SECRETS_KEY.get_secret_value():
            return self.SECRETS_KEY.get_secret_value().encode()
        digest = hashlib.sha256(b"ibx-dev-fernet:" + self.API_TOKEN.get_secret_value().encode()).digest()
        return base64.urlsafe_b64encode(digest)

    def files_signing_key(self) -> bytes:
        """HMAC key for signed download links (/api/v1/files). Derived, so api and worker agree."""
        base = self.SECRETS_KEY or self.API_TOKEN
        return hashlib.sha256(b"ibx-files-link:" + base.get_secret_value().encode()).digest()

    def callback_key(self) -> bytes:
        secret = self.CALLBACK_SECRET or self.API_TOKEN
        return secret.get_secret_value().encode()

    def secret_values(self) -> list[str]:
        """Every live secret value, so the log redactor can scrub it wherever it appears."""
        values = [self.API_TOKEN, self.S3_ACCESS_KEY, self.S3_SECRET_KEY, self.SECRETS_KEY, self.CALLBACK_SECRET,
                  self.ALERT_WEBHOOK_SECRET, self.GITHUB_TOKEN]
        found = [v.get_secret_value() for v in values if v is not None and v.get_secret_value()]
        password = _password_in_url(self.DATABASE_URL)
        if password:
            found.append(password)
        return found

    def safe_snapshot(self) -> dict[str, Any]:
        """Allow-listed settings for manifests and diagnostics. Never contains a secret."""
        return {key: getattr(self, key) for key in SAFE_SNAPSHOT_KEYS}


# The dev placeholders published in this repo (compose.yaml, .env.example). Fine for `docker compose up` on a
# clean machine; refused when APP_ENV is demo or prod. scripts/gen_secrets.sh writes real ones.
DEV_PLACEHOLDER_SECRETS = {"API_TOKEN": "ibx-dev-token", "S3_ACCESS_KEY": "ibx-dev-access", "S3_SECRET_KEY": "ibx-dev-secret"}
DEV_PLACEHOLDER_DB_PASSWORD = "ibx-dev-password"


def ensure_safe_secrets(settings: Settings) -> None:
    """Fail fast if a demo/prod process would run on a secret that is published in the repo."""
    if settings.APP_ENV not in ("demo", "prod"):
        return
    found = [name for name, value in DEV_PLACEHOLDER_SECRETS.items() if getattr(settings, name).get_secret_value() == value]
    if _password_in_url(settings.DATABASE_URL) == DEV_PLACEHOLDER_DB_PASSWORD:
        found.append("POSTGRES_PASSWORD (in DATABASE_URL)")
    if found:
        raise RuntimeError(
            f"APP_ENV={settings.APP_ENV} refuses to start with the default dev secret(s): {', '.join(found)}. "
            "Run scripts/gen_secrets.sh to write fresh values into .env."
        )


def _password_in_url(url: str) -> str | None:
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).password
    except Exception:  # noqa: BLE001 - a malformed URL simply has no password to register
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
