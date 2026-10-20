from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "PICO"
    VERSION: str = "0.1.0"
    DATABASE_URL: str = "sqlite:///./pico.db"
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"

    # Model Provider Settings
    DEFAULT_PROVIDER: str = "mock"  # "mock", "ollama", "cloud"

    # Ollama Local Settings
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    OLLAMA_MODEL: str = "qwen3.5:2b"
    OLLAMA_CONTEXT_WINDOW: int = 4096
    OLLAMA_TIMEOUT_SECONDS: float = 60.0

    # Cloud Provider Settings (Disabled by default)
    CLOUD_ENABLED: bool = False
    CLOUD_BASE_URL: str | None = None
    CLOUD_MODEL: str | None = None
    CLOUD_API_KEY: str | None = None
    # Memory Subsystem Settings
    WORKING_MEMORY_MAX_ITEMS_PER_SESSION: int = 50
    WORKING_MEMORY_DEFAULT_TTL_SECONDS: int = 86400
    SEMANTIC_MEMORY_SEARCH_LIMIT: int = 10
    CONTEXT_COMPILER_MAX_MEMORY_ITEMS: int = 5
    # Network & Security Settings
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    APPROVAL_SECRET_TOKEN: str | None = None
    TRUSTED_APPROVER_IDENTITIES: list[str] = [
        "human_operator",
        "lead_engineer",
        "alice_engineer",
        "admin",
        "pico_owner",
    ]

    # Perception Subsystem Settings
    PERCEPTION_ENABLED: bool = True
    AUDIO_CAPTURE_MAX_SECONDS: int = 60
    AUDIO_MAX_PAYLOAD_BYTES: int = 10 * 1024 * 1024  # 10 MB
    VISION_MAX_IMAGE_BYTES: int = 5 * 1024 * 1024    # 5 MB
    VISION_MAX_DIMENSION: int = 4096
    MOTION_COOLDOWN_SECONDS: float = 2.0
    ENVIRONMENTAL_COOLDOWN_SECONDS: float = 5.0
    PERCEPTION_EVENT_RETENTION_HOURS: int = 24

    # Home Assistant Integration Settings (Disabled by default)
    HOME_ASSISTANT_ENABLED: bool = False
    HOME_ASSISTANT_BASE_URL: str = "http://127.0.0.1:8123"
    HOME_ASSISTANT_ACCESS_TOKEN: str | None = None
    HOME_ASSISTANT_TIMEOUT_SECONDS: float = 10.0
    HOME_ASSISTANT_MAX_ENTITIES_LIMIT: int = 500
    HOME_ASSISTANT_ALLOW_LIVE_SUBSCRIPTION: bool = False
    HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
