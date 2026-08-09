from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.config import get_settings
from app.schemas.agent import (
    AgentPhase,
    AgentSession,
    AgentStatus,
    KnowledgeState,
)
from app.schemas.chat_run import ChatRunStatus
from app.schemas.memory import MemoryCreate, MemoryKind, MemoryScope
from app.services.agent_chat_runner import AgentChatWorker
from app.services.agent_service import AgentService
from app.services.chat_run_service import ChatRunService
from app.services.memory_service import MemoryService


class StreamingOllama:
    async def list_models(self) -> list[dict]:
        return [{"name": "test-model", "model": "test-model"}]

    @staticmethod
    def select_model(
        models: list[dict], requested_model: str | None = None
    ) -> str:
        return requested_model or str(models[0]["model"])

    async def chat(
        self,
        message: str,
        model: str | None = None,
        system: str | None = None,
        num_predict: int | None = None,
    ) -> dict:
        return {
            "model": model or "test-model",
            "message": {"role": "assistant", "content": "summary"},
        }

    async def stream_chat(
        self,
        message: str,
        model: str,
        system: str | None = None,
    ):
        yield {
            "model": model,
            "message": {"role": "assistant", "content": "\u7ed3"},
            "done": False,
        }
        yield {
            "model": model,
            "message": {"role": "assistant", "content": "\u679c"},
            "done": False,
        }
        yield {
            "model": model,
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 12,
            "eval_count": 2,
        }


def _session(
    session_id: str = "session-1",
    project_id: str | None = None,
) -> AgentSession:
    now = datetime.now(timezone.utc)
    return AgentSession(
        id=session_id,
        title="Durable conversation",
        goal="Verify a background answer",
        project_id=project_id,
        phase=AgentPhase.IMPLEMENTATION,
        status=AgentStatus.WAITING_FOR_STAGE,
        execution_mode="local",
        local_percent=100,
        codex_percent=0,
        routing_reason="Local conversation test",
        knowledge_state=KnowledgeState.AVAILABLE,
        created_at=now,
        updated_at=now,
    )


def _worker(tmp_path, monkeypatch) -> AgentChatWorker:
    monkeypatch.setenv("API_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("API_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OLLAMA_MODEL", "test-model")
    get_settings.cache_clear()
    return AgentChatWorker(settings=get_settings(), ollama=StreamingOllama())


def test_worker_persists_output_session_and_shared_memory(
    tmp_path, monkeypatch
) -> None:
    worker = _worker(tmp_path, monkeypatch)
    AgentService(worker.settings.agent_store_path).save(
        _session(project_id="paper-project")
    )
    run = ChatRunService(worker.settings.agent_store_path).create(
        "session-1",
        "\u6211\u504f\u597d\u7528\u5bf9\u6bd4\u8868\u3002",
        request_message_id="request-1",
        metadata={"source": "workbench"},
        idempotency_key="request-1",
    )
    claimed = worker.run_service.claim("test-worker", run_id=run.id)
    assert claimed is not None

    asyncio.run(worker._execute(claimed))

    completed = worker.run_service.get(run.id)
    assert completed is not None
    assert completed.status == ChatRunStatus.COMPLETED
    assert completed.final_text == "\u7ed3\u679c"
    events = worker.run_service.list_events(run.id)
    assert any(event.event_type == "token" for event in events)
    assert events[-1].event_type == "run_completed"

    saved = worker.agent_service.get_session("session-1")
    assert saved is not None
    assert saved.active_operation is None
    assert [message.role for message in saved.messages] == ["user", "assistant"]
    assert saved.messages[-1].content == "\u7ed3\u679c"
    assert saved.context_telemetry.model_output_tokens == 2

    memories = MemoryService(worker.settings.agent_store_path)
    preference = memories.search(
        "\u504f\u597d \u5bf9\u6bd4\u8868",
        kinds=[MemoryKind.PREFERENCE],
    )
    episodes = memories.search(
        "\u7ed3\u679c",
        scope=MemoryScope.PROJECT,
        project_id="paper-project",
        kinds=[MemoryKind.EPISODE],
    )
    assert preference[0].memory.source == "workbench"
    assert episodes[0].memory.source_ref == run.id
    get_settings.cache_clear()


def test_worker_completes_existing_persisted_reply_and_releases_session(
    tmp_path, monkeypatch
) -> None:
    worker = _worker(tmp_path, monkeypatch)
    agent = AgentService(worker.settings.agent_store_path)
    agent.save(_session())
    run = worker.run_service.create(
        "session-1",
        "Resume the same request",
        request_message_id="request-2",
        metadata={"source": "workbench"},
        idempotency_key="request-2",
    )

    def persist_reply(current: AgentSession) -> AgentSession:
        current = agent.append_message(
            current,
            "user",
            "chat",
            run.input_text,
            metadata={"chat_run_id": run.id},
            message_id=run.request_message_id,
        )
        return agent.append_message(
            current,
            "assistant",
            "chat",
            "Already durable",
            metadata={"chat_run_id": run.id},
        )

    agent.mutate("session-1", persist_reply)
    claimed = worker.run_service.claim("test-worker", run_id=run.id)
    assert claimed is not None

    asyncio.run(worker._execute(claimed))

    completed = worker.run_service.get(run.id)
    saved = agent.get_session("session-1")
    assert completed is not None
    assert completed.status == ChatRunStatus.COMPLETED
    assert completed.final_text == "Already durable"
    assert saved is not None and saved.active_operation is None
    assert len([m for m in saved.messages if m.role == "assistant"]) == 1
    get_settings.cache_clear()


def test_stable_global_preferences_are_injected_for_unrelated_queries(
    tmp_path, monkeypatch
) -> None:
    worker = _worker(tmp_path, monkeypatch)
    worker.memory_service.create(
        MemoryCreate(
            scope=MemoryScope.GLOBAL,
            kind=MemoryKind.PREFERENCE,
            content="Always present methodological comparisons in a table.",
            source="user",
        )
    )

    context = worker._memory_context(
        _session(),
        "Summarize the unrelated introduction.",
    )

    assert "methodological comparisons in a table" in context
    get_settings.cache_clear()


def test_unscoped_or_suppressed_conversation_creates_no_episode(
    tmp_path, monkeypatch
) -> None:
    worker = _worker(tmp_path, monkeypatch)
    agent = AgentService(worker.settings.agent_store_path)
    agent.save(_session("unscoped"))
    agent.save(_session("private", project_id="paper-project"))
    unscoped = worker.run_service.create(
        "unscoped",
        "Summarize this temporary note.",
        metadata={"source": "workbench"},
    )
    suppressed = worker.run_service.create(
        "private",
        "I prefer this one request to stay private.",
        metadata={"source": "workbench", "suppress_memory": True},
    )

    for run in (unscoped, suppressed):
        claimed = worker.run_service.claim("test-worker", run_id=run.id)
        assert claimed is not None
        asyncio.run(worker._execute(claimed))

    memories = MemoryService(worker.settings.agent_store_path)
    assert memories.list(kind=MemoryKind.EPISODE) == []
    assert memories.list() == []
    get_settings.cache_clear()


def test_memory_context_separates_global_rules_from_project_history(
    tmp_path, monkeypatch
) -> None:
    worker = _worker(tmp_path, monkeypatch)
    memories = worker.memory_service
    stable = memories.create(
        MemoryCreate(
            scope=MemoryScope.GLOBAL,
            kind=MemoryKind.FACT,
            content="Use evidence-first answers for methodology questions.",
            source="user",
        )
    )
    leaked = memories.create(
        MemoryCreate(
            scope=MemoryScope.GLOBAL,
            kind=MemoryKind.EPISODE,
            content="Methodology sibling branch secret result.",
            source="legacy",
        )
    )
    project_episode = memories.create(
        MemoryCreate(
            scope=MemoryScope.PROJECT,
            project_id="paper-project",
            kind=MemoryKind.EPISODE,
            content="Methodology result approved inside this paper project.",
            source="workbench",
        )
    )
    other_project = memories.create(
        MemoryCreate(
            scope=MemoryScope.PROJECT,
            project_id="other-project",
            kind=MemoryKind.EPISODE,
            content="Methodology result from another private project.",
            source="workbench",
        )
    )

    context = worker._memory_context(
        _session(project_id="paper-project"),
        "methodology result",
    )

    assert stable.content in context
    assert project_episode.content in context
    assert leaked.content not in context
    assert other_project.content not in context
    get_settings.cache_clear()
