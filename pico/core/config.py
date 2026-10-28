import json
from typing import Any
from pydantic import field_validator
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

    # Single-Model Resource Scheduler Settings (Milestone P9.2)
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_MAX_QUEUE_DEPTH: int = 50
    SCHEDULER_QUEUE_TIMEOUT_SECONDS: float = 30.0
    SCHEDULER_MAX_ACTIVE_LOCAL: int = 1
    SCHEDULER_MAX_ACTIVE_CLOUD: int = 5
    SCHEDULER_AGING_INTERVAL_SECONDS: float = 5.0
    SCHEDULER_PROMPT_BUDGET_TOKENS: int = 3328
    SCHEDULER_COMPLETION_BUDGET_TOKENS: int = 512
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

    # Home Assistant Actuation Settings (Strictly disabled by default, Milestone P7.3)
    HOME_ASSISTANT_ACTUATION_ENABLED: bool = False
    HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID: str | None = None
    HOME_ASSISTANT_ACTUATION_TIMEOUT_SECONDS: float = 5.0
    HOME_ASSISTANT_VERIFICATION_TIMEOUT_SECONDS: float = 5.0
    HOME_ASSISTANT_VERIFICATION_POLL_INTERVAL_SECONDS: float = 0.5

    # Home Assistant Stage C Allowlisted Switch Actuation Settings (Milestone P7.4.1)
    HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED: bool = False
    HOME_ASSISTANT_SWITCH_ALLOWLIST: str = "[]"

    # Home Assistant Stage D Climate Awareness & Actuation Settings (Milestones P7.4.2.1 & P7.4.2.2)
    HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED: bool = False
    HOME_ASSISTANT_CLIMATE_ALLOWLIST: str = "[]"
    HOME_ASSISTANT_CLIMATE_GRID_ORIGINS: str = "{}"
    HOME_ASSISTANT_CLIMATE_ACTUATION_ENABLED: bool = False

    # OS & Desktop Application Automation Settings (Milestone P8.1)
    OS_AUTOMATION_ENABLED: bool = True
    OS_APPLICATION_ALLOWLIST: str = "{}"

    # Staged Filesystem Diffs Settings (Milestone P8.2)
    PICO_FILESYSTEM_ALLOWED_ROOTS: str = "{}"
    PICO_FILESYSTEM_MAX_FILE_BYTES: int = 256 * 1024       # 256 KB max file size
    PICO_FILESYSTEM_MAX_LINE_LENGTH: int = 4096            # 4 KB max line length
    PICO_FILESYSTEM_MAX_DIFF_BYTES: int = 256 * 1024       # 256 KB max diff size

    # Sandboxed Browser Automation Settings (Milestone P8.3)
    BROWSER_AUTOMATION_ENABLED: bool = False
    BROWSER_ALLOWED_ORIGINS: list[str] = []
    BROWSER_NAVIGATION_TIMEOUT_SECONDS: float = 15.0
    BROWSER_MAX_OBSERVATION_BYTES: int = 64 * 1024         # 64 KB bounded text extraction
    BROWSER_MAX_LINKS: int = 50                            # max extracted links
    BROWSER_HEADLESS: bool = True
    BROWSER_BLOCK_THIRD_PARTY_SUBRESOURCES: bool = True

    @field_validator("BROWSER_ALLOWED_ORIGINS", mode="before")
    @classmethod
    def parse_allowed_origins(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip()]
                except Exception:
                    pass
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, (list, tuple, set)):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


settings = Settings()
