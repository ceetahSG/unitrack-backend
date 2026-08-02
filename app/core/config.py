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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
