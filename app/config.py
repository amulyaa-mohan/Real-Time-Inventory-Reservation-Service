from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime config comes from env vars (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://inv:inv@localhost:5432/inventory"
    REDIS_URL: str = "redis://localhost:6379/0"
    # Bounds are checked at startup: a bad value (e.g. REAPER_BATCH_SIZE=0, which would
    # make the reaper loop forever) fails fast instead of misbehaving at runtime.
    HOLD_TTL_SECONDS: int = Field(default=120, gt=0)
    REAPER_INTERVAL_SECONDS: float = Field(default=10, gt=0)
    REAPER_BATCH_SIZE: int = Field(default=500, gt=0)
    IDEMPOTENCY_TTL_SECONDS: int = Field(default=86400, gt=0)
    MAX_RESERVE_RETRIES: int = Field(default=5, ge=1)
    DB_POOL_SIZE: int = Field(default=20, ge=1)


settings = Settings()
