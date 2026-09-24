from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./homecam.db"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = "development-only"
    media_root: str = "./media"
    cors_origins: str = "http://localhost:3000"
    ai_provider: str = "mock"
    # AI pipeline (SPEC sections 12-15). Defaults keep HomeCam fully local,
    # deterministic and dependency-free; real backends are strictly opt-in.
    ai_detector_backend: str = "mock"
    ai_detector_model_path: str = ""
    ai_analysis_enabled: bool = True
    embedding_dimensions: int = Field(default=384, ge=8, le=4096)
    best_photo_frames: int = Field(default=3, ge=1, le=10)
    best_photo_enabled: bool = True
    zone_min_overlap: float = Field(default=0.3, gt=0.0, le=1.0)
    parked_vehicle_seconds: float = Field(default=60.0, gt=0.0)
    audio_detection_enabled: bool = False
    audio_energy_threshold: float = Field(default=0.02, gt=0.0, le=1.0)
    activity_correlation_enabled: bool = True
    activity_correlation_window_seconds: float = Field(default=120.0, gt=0.0)
    # Continuous event ingestion (SPEC section 12): periodically snapshots
    # every online camera and runs it through the configured detector so
    # events exist without a human manually POSTing /mock/events. Disabled
    # by default (matches every other opt-in AI knob above) so tests/local
    # dev never spawn a background polling loop unexpectedly; the deployed
    # Azure API turns this on explicitly.
    event_ingestion_enabled: bool = False
    event_poll_interval_seconds: float = Field(default=20.0, gt=0.0)
    event_cooldown_seconds: float = Field(default=120.0, gt=0.0)
    mediamtx_url: str = "http://localhost:8889"
    # Own public FQDN (e.g. the Container App's https://... ingress URL). Used
    # to rewrite private-only (Tailscale tailnet / container-localhost) live
    # stream URLs into a public HLS proxy path a real browser can reach.
    public_api_base_url: str | None = None
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
    # Edge connector mode (Home Assistant/Raspberry Pi bridge, see
    # docs/edge-connector.md). "direct" keeps the existing behavior above;
    # "edge" talks to a local edge connector over a private VPN/overlay
    # (Tailscale recommended) instead of the raw Dahua HTTP/RTSP surface.
    dahua_mode: str = "direct"
    dahua_edge_url: str | None = None
    dahua_edge_token: str | None = None
    dahua_edge_timeout_seconds: float = Field(default=5.0, gt=0)
    dahua_edge_retries: int = Field(default=1, ge=0, le=5)
    eufy_enabled: bool = False
    eufy_adapter_url: str | None = None
    eufy_adapter_token: str | None = None
    eufy_timeout_seconds: float = Field(default=10.0, gt=0)
    eufy_retries: int = Field(default=1, ge=0, le=5)
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
