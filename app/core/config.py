from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"

    # Postgres
    postgres_user: str = "unitrack"
    postgres_password: str = "unitrack"
    postgres_db: str = "unitrack"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    # Empty in dev (Redis runs without auth); set in production where Redis
    # runs with requirepass. Empty means "no password", i.e. current behaviour.
    redis_password: str = ""

    # Elasticsearch
    elasticsearch_url: str = "http://elasticsearch:9200"
    gps_index: str = "gps_points"
    # Empty in dev (xpack.security off); set in prod where xpack.security is on.
    elasticsearch_user: str = ""
    elasticsearch_password: str = ""

    # Auth
    jwt_secret: str = "change-me-in-prod"
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 30

    # Identity — varsity domain allow-list for student signup
    allowed_student_email_domains: str = "ulab.edu.bd"

    # --- Payments (SSLCommerz) ---
    # `store_passwd` is a credential even in sandbox: it authenticates the
    # validation call that decides whether a ticket gets issued. Real values
    # live in .env, which is gitignored.
    sslcommerz_store_id: str = ""
    sslcommerz_store_password: str = ""
    # False targets sandbox.sslcommerz.com. Flip only with live merchant
    # credentials — sandbox ids do not authenticate against the live host.
    sslcommerz_live: bool = False
    # Where the gateway sends the student back. Must be reachable by their
    # browser, so it is the *public* origin of this API, not a container name.
    public_base_url: str = "http://localhost:8000"
    # Where the student's browser lands after we have settled the order. Empty
    # means the API renders its own minimal confirmation instead.
    checkout_return_url: str = ""

    # Browser origins allowed to call this API, comma-separated. Empty means no
    # CORS headers at all, which is the correct default for a server whose only
    # clients are the Flutter app and curl — a browser blocks cross-origin reads
    # unless the server opts in, so shipping nothing is shipping the safe thing.
    # Set it when unitrack-web exists; there is deliberately no wildcard.
    cors_origins: str = ""

    # Operations
    # The fleet's local timezone. Storage is UTC throughout; this is only used
    # to decide which *service day* a trip belongs to. Deriving that from UTC
    # would roll the day over at 06:00 local, splitting a morning's trips
    # across two dates and quietly corrupting every ridership report.
    service_timezone: str = "Asia/Dhaka"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def student_email_domains(self) -> set[str]:
        raw = self.allowed_student_email_domains.split(",")
        return {d.strip().lower() for d in raw if d.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        """Parsed `cors_origins`. A `*` entry is dropped, not honoured.

        Wildcard CORS on an authenticated API means any page on the internet can
        script requests carrying the visitor's bearer token. It is never what
        this service wants, so it cannot be switched on by a typo in an env var.
        """
        raw = self.cors_origins.split(",")
        return [o.strip() for o in raw if o.strip() and o.strip() != "*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
