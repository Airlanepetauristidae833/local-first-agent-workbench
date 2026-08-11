import asyncio
from datetime import datetime, timezone

import httpx

from app.config import get_settings
from app.main import app
from app.routers import knowledge as knowledge_router
from app.routers import system as system_router
from app.schemas.agent import (
    AgentPhase,
    AgentSession,
    AgentStage,
    AgentStatus,
    KnowledgeState,
)
from app.schemas.memory import MemoryCreate, MemoryKind, MemoryScope
from app.services.agent_service import AgentService
from app.services.memory_service import MemoryService
from app.services.ollama_client import OllamaResponseError, get_ollama_client


def test_probe_accepts_successful_plain_text_liveness(monkeypatch) -> None:
    class PlainTextClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(
            self,
            url: str,
            params: dict | None = None,
            headers: dict | None = None,
        ) -> httpx.Response:
            assert headers == {
                "X-Forwarded-For": "127.0.0.1",
                "X-Real-IP": "127.0.0.1",
            }
            return httpx.Response(200, text="OK")

    monkeypatch.setattr(system_router.httpx, "AsyncClient", PlainTextClient)

    assert asyncio.run(system_router._probe("http://search:8080/healthz")) == (
        True,
        {},
    )


def test_system_overview_uses_searxng_liveness_without_searching(
    client, monkeypatch
) -> None:
    calls: list[str] = []

    async def fake_probe(url: str, params: dict | None = None) -> tuple[bool, dict]:
        calls.append(url)
        if url.endswith("/health"):
            return True, {"status": "ok", "index": {"stale_projects": 0}}
        return True, {}

    monkeypatch.setattr(system_router, "_probe", fake_probe)
    response = client.get("/api/v1/system/overview")

    assert response.status_code == 200
    assert response.json()["services"]["search"]["healthy"] is True
    assert any(url.endswith("/healthz") for url in calls)
    assert not any(url.endswith("/search") or "/search?" in url for url in calls)


def test_system_overview_does_not_expose_ollama_error_details(
    client, monkeypatch
) -> None:
    secret = "private upstream body: prompt and C:" + r"\secret\model.bin"
    log_calls: list[tuple[str, tuple[object, ...]]] = []

    class FailingOllama:
        async def list_models(self):
            raise OllamaResponseError(500, secret)

    async def healthy_probe(
        url: str, params: dict | None = None
    ) -> tuple[bool, dict]:
        if url.endswith("/health"):
            return True, {"status": "ok", "index": {"stale_projects": 0}}
        return True, {}

    def capture_log(message: str, *args: object, **_kwargs: object) -> None:
        log_calls.append((message, args))

    monkeypatch.setattr(system_router, "_probe", healthy_probe)
    monkeypatch.setattr(system_router.logger, "warning", capture_log)
    app.dependency_overrides[get_ollama_client] = FailingOllama

    response = client.get("/api/v1/system/overview")

    assert response.status_code == 200
    ollama = response.json()["services"]["ollama"]
    assert ollama == {
        "healthy": False,
        "model": None,
        "models": 0,
        "error": "Ollama status is unavailable",
    }
    assert secret not in response.text
    assert len(log_calls) == 1
    rendered_log = log_calls[0][0] % log_calls[0][1]
    assert "event=ollama_overview_failed" in rendered_log
    assert "error_type=OllamaResponseError" in rendered_log
    assert "upstream_status=500" in rendered_log
    assert secret not in rendered_log


def make_session(identifier: str = "managed-session", project_id: str | None = None) -> AgentSession:
    now = datetime.now(timezone.utc)
    return AgentSession(
        id=identifier,
        title="Manageable task",
        goal="Verify the task lifecycle",
        project_id=project_id,
        phase=AgentPhase.IMPLEMENTATION,
        status=AgentStatus.WAITING_FOR_STAGE,
        execution_mode="local",
        local_percent=100,
        codex_percent=0,
        routing_reason="The local model can complete this task",
        knowledge_state=KnowledgeState.AVAILABLE,
        created_at=now,
        updated_at=now,
    )


def test_session_can_be_renamed_archived_restored_and_deleted(client) -> None:
    service = AgentService(get_settings().agent_store_path)
    session = service.save(make_session())

    renamed = client.patch(
        f"/api/v1/agent/sessions/{session.id}",
        json={
            "title": "Renamed task",
            "archived": True,
            "codex_context_consent": True,
        },
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Renamed task"
    assert renamed.json()["archived_at"] is not None
    assert renamed.json()["codex_context_consent"] is True
    assert client.get("/api/v1/agent/sessions").json()["count"] == 0
    assert (
        client.get("/api/v1/agent/sessions?include_archived=true").json()["count"]
        == 1
    )

    restored = client.patch(
        f"/api/v1/agent/sessions/{session.id}", json={"archived": False}
    )
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None

    summaries = client.get(
        "/api/v1/agent/sessions/summaries?include_archived=true"
    )
    assert summaries.status_code == 200
    summary = summaries.json()["items"][0]
    assert summary["id"] == session.id
    assert summary["stage_count"] == 0
    assert summary["completed_stages"] == 0
    assert "messages" not in summary

    wrong = client.request(
        "DELETE",
        f"/api/v1/agent/sessions/{session.id}",
        json={"confirm_title": "Does not match"},
    )
    assert wrong.status_code == 422
    deleted = client.request(
        "DELETE",
        f"/api/v1/agent/sessions/{session.id}",
        json={"confirm_title": "Renamed task"},
    )
    assert deleted.status_code == 200
    assert service.get_session(session.id) is None


def test_knowledge_delete_requires_detaching_linked_sessions(
    client, monkeypatch
) -> None:
    service = AgentService(get_settings().agent_store_path)
    session = service.save(make_session("linked-session", "paper-library"))
    memory_service = MemoryService(get_settings().agent_store_path)
    project_memory = memory_service.create(
        MemoryCreate(
            scope=MemoryScope.PROJECT,
            project_id="paper-library",
            kind=MemoryKind.DECISION,
            content="Use peer-reviewed sources for this paper.",
            source="test",
        )
    )
    global_memory = memory_service.create(
        MemoryCreate(
            kind=MemoryKind.PREFERENCE,
            content="Prefer concise progress summaries.",
            source="test",
        )
    )
    calls: list[tuple[str, str, dict | None]] = []

    async def fake_request(
        method: str,
        path: str,
        payload: dict | None = None,
        timeout_seconds: float = 60.0,
        params: dict | None = None,
    ) -> dict:
        calls.append((method, path, payload))
        return {
            "deleted": True,
            "id": "paper-library",
            "name": "Paper knowledge",
            "external_sources_deleted": False,
        }

    monkeypatch.setattr(knowledge_router, "_request", fake_request)
    blocked = client.request(
        "DELETE",
        "/api/v1/knowledge/projects/paper-library",
        json={"confirm_name": "Paper knowledge"},
    )
    assert blocked.status_code == 409
    assert calls == []

    deleted = client.request(
        "DELETE",
        "/api/v1/knowledge/projects/paper-library",
        json={
            "confirm_name": "Paper knowledge",
            "detach_sessions": True,
            "trash_managed_files": True,
        },
    )
    assert deleted.status_code == 200
    assert deleted.json()["detached_sessions"] == 1
    assert deleted.json()["deleted_memories"] == 1
    assert calls[0][0:2] == ("DELETE", "/projects/paper-library")
    assert memory_service.get(project_memory.id) is None
    assert memory_service.get(global_memory.id) == global_memory
    detached = service.get_session(session.id)
    assert detached is not None
    assert detached.project_id is None
    assert detached.archived_at is not None
    assert detached.knowledge_state == KnowledgeState.MISSING
    assert detached.knowledge_matches == []
    assert detached.selected_source_ids == []
    assert detached.research_note is None
    assert detached.active_operation is None
    assert detached.messages[-1].kind == "management"


def test_global_knowledge_search_is_available_through_the_gateway(
    client, monkeypatch
) -> None:
    async def fake_request(
        method: str,
        path: str,
        payload: dict | None = None,
        timeout_seconds: float = 60.0,
        params: dict | None = None,
    ) -> dict:
        assert (method, path) == ("POST", "/search")
        assert payload == {"query": "shared evidence", "limit": 3}
        return {"query": payload["query"], "matches": []}

    monkeypatch.setattr(knowledge_router, "_request", fake_request)
    response = client.post(
        "/api/v1/knowledge/search",
        json={"query": "shared evidence", "limit": 3},
    )
    assert response.status_code == 200
    assert response.json() == {"query": "shared evidence", "matches": []}


def test_running_and_archived_sessions_reject_unsafe_mutations(client) -> None:
    service = AgentService(get_settings().agent_store_path)
    now = datetime.now(timezone.utc)
    running = service.save(
        make_session("running-session").model_copy(
            update={
                "status": AgentStatus.LOCAL_RUNNING,
                "active_operation": "local-stage:0",
                "operation_started_at": now,
                "stages": [
                    AgentStage(
                        id="stage-1",
                        title="Execution",
                        description="Complete the stage",
                        owner="local",
                        status="running",
                        started_at=now,
                    )
                ],
            }
        )
    )
    blocked_delete = client.request(
        "DELETE",
        f"/api/v1/agent/sessions/{running.id}",
        json={"confirm_title": running.title},
    )
    assert blocked_delete.status_code == 409
    blocked_archive = client.patch(
        f"/api/v1/agent/sessions/{running.id}", json={"archived": True}
    )
    assert blocked_archive.status_code == 409
    blocked_consent = client.patch(
        f"/api/v1/agent/sessions/{running.id}",
        json={"codex_context_consent": True},
    )
    assert blocked_consent.status_code == 409

    archived = service.save(
        make_session("archived-session").model_copy(
            update={
                "archived_at": now,
                "stages": [
                    AgentStage(
                        id="stage-1",
                        title="Execution",
                        description="Complete the stage",
                        owner="local",
                    )
                ],
            }
        )
    )
    blocked_advance = client.post(
        f"/api/v1/agent/sessions/{archived.id}/advance", json={}
    )
    blocked_chat = client.post(
        f"/api/v1/agent/sessions/{archived.id}/messages",
        json={"content": "Continue"},
    )
    assert blocked_advance.status_code == 409
    assert blocked_chat.status_code == 410
