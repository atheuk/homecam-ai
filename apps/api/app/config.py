from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./homecam.db"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "development-only"
    ai_provider: str = "mock"
    mediamtx_url: str = "http://localhost:8889"
    low_battery_threshold: int = 20
    session_ttl_minutes: int = 60 * 12
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
