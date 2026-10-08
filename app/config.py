from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime config comes from env vars (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://inv:inv@localhost:5432/inventory"
    REDIS_URL: str = "redis://localhost:6379/0"
    HOLD_TTL_SECONDS: int = 120
    REAPER_INTERVAL_SECONDS: int = 10
    REAPER_BATCH_SIZE: int = 500
    IDEMPOTENCY_TTL_SECONDS: int = 86400
    MAX_RESERVE_RETRIES: int = 5
    DB_POOL_SIZE: int = 20


settings = Settings()
