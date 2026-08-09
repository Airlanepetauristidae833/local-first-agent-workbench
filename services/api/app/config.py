import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str
    app_version: str
    data_dir: Path
    log_dir: Path
    log_level: str
    ollama_base_url: str
    ollama_model: str | None
    ollama_connect_timeout_seconds: float
    ollama_request_timeout_seconds: float
    ollama_first_token_timeout_seconds: float
    ollama_stream_idle_timeout_seconds: float
    ollama_keep_alive: str
    ollama_think: bool
    ollama_context_length: int
    ollama_num_predict: int
    agent_context_budget_tokens: int
    agent_context_compact_trigger_tokens: int
    agent_context_recent_messages: int
    agent_context_summary_max_tokens: int
    workspace_root: Path
    knowledge_service_url: str
    search_service_url: str
    public_open_webui_url: str
    public_agent_url: str

    @property
    def task_store_path(self) -> Path:
        return self.data_dir / "tasks.json"

    @property
    def orchestrator_store_path(self) -> Path:
        return self.data_dir / "orchestrator.sqlite3"

    @property
    def agent_store_path(self) -> Path:
        return self.data_dir / "personal-agent.sqlite3"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    ollama_model = os.getenv("OLLAMA_MODEL", "").strip() or None
    return Settings(
        app_name=os.getenv("APP_NAME", "Local-First Agent API"),
        app_version=os.getenv("APP_VERSION", "1.0.0"),
        data_dir=Path(os.getenv("API_DATA_DIR", "/app/data")),
        log_dir=Path(os.getenv("API_LOG_DIR", "/app/logs")),
        log_level=os.getenv("API_LOG_LEVEL", "INFO").upper(),
        ollama_base_url=os.getenv(
            "OLLAMA_BASE_URL",
            "http://host.docker.internal:11434",
        ).rstrip("/"),
        ollama_model=ollama_model,
        ollama_connect_timeout_seconds=float(
            os.getenv("OLLAMA_CONNECT_TIMEOUT_SECONDS", "3")
        ),
        ollama_request_timeout_seconds=float(
            os.getenv("OLLAMA_REQUEST_TIMEOUT_SECONDS", "300")
        ),
        ollama_first_token_timeout_seconds=float(
            os.getenv("OLLAMA_FIRST_TOKEN_TIMEOUT_SECONDS", "180")
        ),
        ollama_stream_idle_timeout_seconds=float(
            os.getenv("OLLAMA_STREAM_IDLE_TIMEOUT_SECONDS", "90")
        ),
        ollama_keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", "5m"),
        ollama_think=os.getenv("OLLAMA_THINK", "false").lower()
        in {"1", "true", "yes", "on"},
        ollama_context_length=int(os.getenv("OLLAMA_CONTEXT_LENGTH", "16384")),
        ollama_num_predict=int(os.getenv("OLLAMA_NUM_PREDICT", "4096")),
        agent_context_budget_tokens=int(
            os.getenv("AGENT_CONTEXT_BUDGET_TOKENS", "10000")
        ),
        agent_context_compact_trigger_tokens=int(
            os.getenv("AGENT_CONTEXT_COMPACT_TRIGGER_TOKENS", "7000")
        ),
        agent_context_recent_messages=int(
            os.getenv("AGENT_CONTEXT_RECENT_MESSAGES", "8")
        ),
        agent_context_summary_max_tokens=int(
            os.getenv("AGENT_CONTEXT_SUMMARY_MAX_TOKENS", "1200")
        ),
        workspace_root=Path(os.getenv("WORKSPACE_ROOT", "/workspaces")),
        knowledge_service_url=os.getenv("KNOWLEDGE_SERVICE_URL", "http://knowledge:8100").rstrip("/"),
        search_service_url=os.getenv("SEARCH_SERVICE_URL", "http://search:8080").rstrip("/"),
        public_open_webui_url=os.getenv(
            "PUBLIC_OPEN_WEBUI_URL", "http://127.0.0.1:3000"
        ).rstrip("/"),
        public_agent_url=os.getenv(
            "PUBLIC_AGENT_URL", "http://127.0.0.1:8000/console"
        ),
    )
