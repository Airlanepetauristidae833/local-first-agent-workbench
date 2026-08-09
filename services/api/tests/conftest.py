from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services.ollama_client import (
    OllamaResponseError,
    get_ollama_client,
)
from app.services.service_registry import get_service_registry
from app.services.task_service import get_task_service
from app.services.workspace_service import get_workspace_service


class FakeOllamaClient:
    def __init__(self) -> None:
        self.models = [
            {
                "name": "test-model",
                "model": "test-model",
                "modified_at": "2026-07-23T00:00:00Z",
                "size": 123,
                "digest": "test-digest",
                "details": {"parameter_size": "1B"},
                "capabilities": ["completion"],
            }
        ]

    async def list_models(self) -> list[dict]:
        return self.models

    def select_model(
        self,
        models: list[dict],
        requested_model: str | None = None,
    ) -> str:
        selected = requested_model or "test-model"
        installed = {model["model"] for model in models}
        if selected not in installed:
            raise OllamaResponseError(404, f"model '{selected}' is not installed")
        return selected

    async def chat(
        self,
        message: str,
        model: str | None = None,
        system: str | None = None,
    ) -> dict:
        selected = self.select_model(self.models, model)
        return {
            "model": selected,
            "message": {"role": "assistant", "content": f"reply: {message}"},
            "done": True,
            "done_reason": "stop",
            "total_duration": 100,
            "eval_count": 2,
            "eval_duration": 50,
        }

    async def stream_chat(
        self,
        message: str,
        model: str,
        system: str | None = None,
    ):
        yield {
            "model": model,
            "message": {"role": "assistant", "content": "\u76d0"},
            "done": False,
        }
        yield {
            "model": model,
            "message": {"role": "assistant", "content": "\u57ce"},
            "done": False,
        }
        yield {
            "model": model,
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "total_duration": 100,
            "eval_count": 2,
            "eval_duration": 50,
        }


@pytest.fixture
def ollama_client() -> FakeOllamaClient:
    return FakeOllamaClient()


@pytest.fixture
def client(tmp_path, monkeypatch, ollama_client) -> Iterator[TestClient]:
    workspace_root = tmp_path / "workspaces"
    sample = workspace_root / "sample-workspace"
    (sample / "src").mkdir(parents=True)
    (sample / "node_modules").mkdir()
    (sample / ".ai-workspace.json").write_text(
        """{
  "id": "sample-workspace",
  "name": "Sample Workspace",
  "description": "Test-only read-only workspace",
  "capabilities": ["inspect", "search"],
  "policy": {
    "file_access": "read-only",
    "command_execution": "disabled",
    "command_allowlist": [],
    "human_approval_required_for": ["write", "command"]
  }
}
""",
        encoding="utf-8",
    )
    (sample / "README.md").write_text("# Sample\n", encoding="utf-8")
    (sample / "src" / "main.py").write_text("print('ready')\n", encoding="utf-8")
    (sample / "node_modules" / "ignored.js").write_text(
        "ignored\n",
        encoding="utf-8",
    )
    unregistered = workspace_root / "unregistered"
    unregistered.mkdir()
    (unregistered / "private.txt").write_text("not registered\n", encoding="utf-8")

    monkeypatch.setenv("API_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("API_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("API_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("OLLAMA_MODEL", "test-model")
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    get_settings.cache_clear()
    get_task_service.cache_clear()
    get_service_registry.cache_clear()
    get_ollama_client.cache_clear()
    get_workspace_service.cache_clear()
    app.dependency_overrides[get_ollama_client] = lambda: ollama_client
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_ollama_client.cache_clear()
    get_task_service.cache_clear()
    get_service_registry.cache_clear()
    get_workspace_service.cache_clear()
    get_settings.cache_clear()
