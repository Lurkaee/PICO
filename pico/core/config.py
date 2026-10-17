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
    CLOUD_TIMEOUT_SECONDS: float = 60.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
