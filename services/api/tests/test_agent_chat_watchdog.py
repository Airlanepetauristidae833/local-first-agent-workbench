from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.config import get_settings
from app.schemas.agent import (
    AgentPhase,
    AgentSession,
    AgentStatus,
    KnowledgeState,
)
from app.services.agent_chat_runner import AgentChatWorker
from app.services.agent_service import AgentService
from app.services.chat_run_service import ChatRunService
from app.services.ollama_client import OllamaTimeoutError


class BlockingOllama:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()

    async def list_models(self) -> list[dict]:
        return [{"name": "test-model", "model": "test-model"}]

    @staticmethod
    def select_model(models: list[dict], requested_model: str | None = None) -> str:
        return requested_model or str(models[0]["model"])

    async def chat(self, **kwargs) -> dict:
        return {"model": "test-model", "message": {"content": "summary"}}

    async def stream_chat(self, **kwargs):
        try:
            self.entered.set()
            await asyncio.Event().wait()
            yield {}
        finally:
            self.closed.set()


class IdleAfterTokenOllama(BlockingOllama):
    async def stream_chat(self, **kwargs):
        try:
            yield {
                "message": {"role": "assistant", "content": "started"},
                "done": False,
            }
            self.entered.set()
            await asyncio.Event().wait()
            yield {}
        finally:
            self.closed.set()


class RecoveringOllama(BlockingOllama):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def stream_chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            try:
                self.entered.set()
                await asyncio.Event().wait()
                yield {}
            finally:
                self.closed.set()
            return
        yield {
            "message": {"role": "assistant", "content": "recovered"},
            "done": False,
        }
        yield {
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 4,
            "eval_count": 1,
        }


def make_worker(tmp_path, monkeypatch, ollama: BlockingOllama) -> AgentChatWorker:
    monkeypatch.setenv("API_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("API_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OLLAMA_MODEL", "test-model")
    monkeypatch.setenv("KNOWLEDGE_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SEARCH_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("OLLAMA_FIRST_TOKEN_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("OLLAMA_STREAM_IDLE_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()
    worker = AgentChatWorker(settings=get_settings(), ollama=ollama)
    now = datetime.now(timezone.utc)
    AgentService(worker.settings.agent_store_path).save(
        AgentSession(
            id="watchdog-session",
            title="Watchdog",
            goal="Verify cancellation",
            phase=AgentPhase.IMPLEMENTATION,
            status=AgentStatus.WAITING_FOR_STAGE,
            execution_mode="local",
            local_percent=100,
            codex_percent=0,
            routing_reason="Local test",
            knowledge_state=KnowledgeState.AVAILABLE,
            created_at=now,
            updated_at=now,
        )
    )
    return worker


def claim_run(worker: AgentChatWorker):
    service = ChatRunService(worker.settings.agent_store_path)
    created = service.create(
        "watchdog-session",
        "Generate a response",
        metadata={"source": "workbench", "suppress_memory": True},
    )
    claimed = service.claim("test-worker", run_id=created.id)
    assert claimed is not None
    return claimed


def test_running_cancel_closes_blocked_ollama_stream(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        ollama = BlockingOllama()
        worker = make_worker(tmp_path, monkeypatch, ollama)
        run = claim_run(worker)
        task = asyncio.create_task(worker._execute(run))
        await asyncio.wait_for(ollama.entered.wait(), timeout=1)
        worker.run_service.cancel(run.id)
        with pytest.raises(RuntimeError, match="cancelled"):
            await asyncio.wait_for(task, timeout=2)
        await asyncio.wait_for(ollama.closed.wait(), timeout=1)

    asyncio.run(scenario())
    get_settings.cache_clear()


def test_first_token_watchdog_closes_stalled_stream(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        ollama = BlockingOllama()
        worker = make_worker(tmp_path, monkeypatch, ollama)
        run = claim_run(worker)
        with pytest.raises(OllamaTimeoutError, match="first token"):
            await asyncio.wait_for(worker._execute(run), timeout=2)
        await asyncio.wait_for(ollama.closed.wait(), timeout=1)

    asyncio.run(scenario())
    get_settings.cache_clear()


def test_idle_watchdog_preserves_first_chunk_and_closes_stream(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        ollama = IdleAfterTokenOllama()
        worker = make_worker(tmp_path, monkeypatch, ollama)
        run = claim_run(worker)
        with pytest.raises(OllamaTimeoutError, match="stream idle"):
            await asyncio.wait_for(worker._execute(run), timeout=2)
        persisted = worker.run_service.get(run.id)
        assert persisted is not None
        assert persisted.partial_text == "started"
        await asyncio.wait_for(ollama.closed.wait(), timeout=1)

    asyncio.run(scenario())
    get_settings.cache_clear()


def test_watchdog_retries_once_without_duplicate_output(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        ollama = RecoveringOllama()
        worker = make_worker(tmp_path, monkeypatch, ollama)
        run = ChatRunService(worker.settings.agent_store_path).create(
            "watchdog-session",
            "Recover automatically",
            metadata={"source": "workbench", "suppress_memory": True},
        )
        await worker.start()
        worker.notify()
        try:
            for _ in range(40):
                current = worker.run_service.get(run.id)
                if current is not None and current.status.value == "completed":
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("watchdog retry did not complete")
        finally:
            await worker.stop()

        completed = worker.run_service.get(run.id)
        assert completed is not None
        assert completed.attempt_no == 2
        assert completed.final_text == "recovered"
        events = worker.run_service.list_events(run.id)
        assert len([event for event in events if event.event_type == "run_requeued"]) == 1
        assert completed.partial_text == "recovered"

    asyncio.run(scenario())
    get_settings.cache_clear()
