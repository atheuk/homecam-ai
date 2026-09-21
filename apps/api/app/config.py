from pydantic import Field
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
    dahua_enabled: bool = False
    dahua_scheme: str = "http"
    dahua_host: str | None = None
    dahua_port: int = 80
    dahua_username: str | None = None
    dahua_password: str | None = None
    dahua_serial: str = "5J006FCPAZ6B52A"
    dahua_channels: str = ""
    dahua_timeout_seconds: float = Field(default=5.0, gt=0)
    dahua_retries: int = Field(default=1, ge=0, le=5)
    eufy_enabled: bool = False
    eufy_adapter_url: str | None = None
    eufy_adapter_token: str | None = None
    eufy_timeout_seconds: float = Field(default=10.0, gt=0)
    eufy_retries: int = Field(default=1, ge=0, le=5)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
