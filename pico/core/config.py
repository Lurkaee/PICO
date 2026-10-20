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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
