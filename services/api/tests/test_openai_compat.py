from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers import openai_compat
from app.schemas.chat_run import ChatRunStatus
from app.schemas.memory import MemoryCreate, MemoryKind, MemoryScope
from app.services.agent_chat_runner import AgentChatWorker
from app.services.agent_service import AgentService
from app.services.chat_run_service import ChatRunService
from app.services.external_chat_link_service import ExternalChatLinkService
from app.services.memory_service import MemoryService

TOKEN = "bridge-test-token-with-at-least-32-characters"


class CompletingNotifier:
    def __init__(self, runs: ChatRunService) -> None:
        self.runs = runs
        self.executions = 0

    def __call__(self) -> None:
        claimed = self.runs.claim("openai-compat-test-worker")
        if claimed is None:
            return
        self.executions += 1
        assert claimed.attempt_id
        self.runs.append_event(
            claimed.id,
            "token",
            {"content": "durable answer", "model": "test-model"},
            partial_text="durable answer",
            attempt_id=claimed.attempt_id,
            idempotency_key=f"test-token:{claimed.attempt_id}",
        )
        self.runs.complete(
            claimed.id,
            attempt_id=claimed.attempt_id,
            idempotency_key=f"test-complete:{claimed.attempt_id}",
        )


class CapturingOllama:
    def __init__(self) -> None:
        self.system_prompt = ""

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
        self.system_prompt = system or ""
        yield {
            "model": model,
            "message": {"role": "assistant", "content": "shared result"},
            "done": False,
        }
        yield {
            "model": model,
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 20,
            "eval_count": 2,
        }


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("API_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("API_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv(openai_compat.BRIDGE_TOKEN_ENV, TOKEN)
    get_settings.cache_clear()
    database = get_settings().agent_store_path
    agents = AgentService(database)
    runs = ChatRunService(database)
    links = ExternalChatLinkService(database)
    memories = MemoryService(database)
    agents.initialize()
    runs.initialize()
    links.initialize()
    memories.initialize()
    notifier = CompletingNotifier(runs)

    app = FastAPI()
    app.include_router(openai_compat.router)
    app.dependency_overrides[openai_compat.get_agent_service] = lambda: agents
    app.dependency_overrides[openai_compat.get_chat_run_service] = lambda: runs
    app.dependency_overrides[
        openai_compat.get_external_chat_link_service
    ] = lambda: links
    app.dependency_overrides[
        openai_compat.get_worker_notifier
    ] = lambda: notifier
    with TestClient(app) as client:
        yield client, agents, runs, links, memories, notifier, database
    get_settings.cache_clear()


def _headers(**updates: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "X-OpenWebUI-User-Id": "opaque-user-1",
        "X-OpenWebUI-Chat-Id": "opaque-chat-1",
        "X-OpenWebUI-Message-Id": "opaque-response-message-1",
        "X-OpenWebUI-User-Message-Id": "opaque-user-message-1",
        "X-OpenWebUI-User-Message-Parent-Id": "opaque-parent-1",
    }
    headers.update(updates)
    return headers


def _body(
    *,
    stream: bool,
    text: str = "Remember this request",
    suppress_memory: bool | None = None,
) -> dict:
    body = {
        "model": "personal-agent",
        "messages": [{"role": "user", "content": text}],
        "stream": stream,
        "temperature": 0.2,
    }
    if suppress_memory is not None:
        body["suppress_memory"] = suppress_memory
    return body


def test_bridge_requires_bearer_token_and_lists_one_model(bridge) -> None:
    client = bridge[0]
    assert client.get("/v1/models").status_code == 401
    assert (
        client.get(
            "/v1/models", headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )

    response = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {
                "id": "personal-agent",
                "object": "model",
                "created": 0,
                "owned_by": "local-workbench",
            }
        ],
    }


def test_non_streaming_bridge_is_idempotent_and_anonymous(bridge) -> None:
    client, agents, runs, links, _, notifier, database = bridge
    headers = _headers(
        **{
            "X-OpenWebUI-User-Name": "Sensitive Display 9381",
            "X-OpenWebUI-User-Email": "sensitive.9381@example.invalid",
        }
    )
    first = client.post(
        "/v1/chat/completions",
        headers=headers,
        json=_body(stream=False),
    )
    second = client.post(
        "/v1/chat/completions",
        headers=headers,
        json=_body(stream=False),
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["choices"][0]["message"]["content"] == "durable answer"
    assert first.json()["id"] == second.json()["id"]
    assert notifier.executions == 1
    assert len(agents.list_sessions()) == 1
    session = agents.list_sessions()[0]
    assert session.title.startswith("Open WebUI bridge ")
    assert session.goal == "Private Personal Agent bridge conversation."

    chat_link = links.get_chat(
        source="open-webui",
        opaque_user_id="opaque-user-1",
        external_chat_id="opaque-chat-1",
    )
    message_link = links.get_message(
        source="open-webui",
        opaque_user_id="opaque-user-1",
        external_chat_id="opaque-chat-1",
        external_message_id="opaque-response-message-1",
    )
    assert chat_link is not None
    assert message_link is not None
    assert runs.get(message_link.job_id) is not None
    assert chat_link.opaque_user_hash != "opaque-user-1"
    assert chat_link.external_chat_hash != "opaque-chat-1"

    stored = database.read_bytes()
    for secret_value in (
        b"opaque-user-1",
        b"opaque-chat-1",
        b"opaque-response-message-1",
        b"opaque-user-message-1",
        b"Sensitive Display 9381",
        b"sensitive.9381@example.invalid",
    ):
        assert secret_value not in stored


def test_streaming_bridge_replays_completed_job_without_new_execution(bridge) -> None:
    client, _, runs, links, _, notifier, _ = bridge
    first = client.post(
        "/v1/chat/completions",
        headers=_headers(),
        json=_body(stream=True),
    )
    second = client.post(
        "/v1/chat/completions",
        headers=_headers(),
        json=_body(stream=True),
    )

    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"].startswith("text/event-stream")
    assert "durable answer" in first.text
    assert "\"finish_reason\":\"stop\"" in first.text
    assert first.text.endswith("data: [DONE]\n\n")
    assert first.text == second.text
    assert notifier.executions == 1
    link = links.get_message(
        source="open-webui",
        opaque_user_id="opaque-user-1",
        external_chat_id="opaque-chat-1",
        external_message_id="opaque-response-message-1",
    )
    assert link is not None
    assert runs.get(link.job_id).status == ChatRunStatus.COMPLETED


def test_same_external_message_with_changed_input_is_a_conflict(bridge) -> None:
    client = bridge[0]
    assert (
        client.post(
            "/v1/chat/completions",
            headers=_headers(),
            json=_body(stream=False, text="first input"),
        ).status_code
        == 200
    )
    conflict = client.post(
        "/v1/chat/completions",
        headers=_headers(),
        json=_body(stream=False, text="changed input"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"]["code"] == (
        "bridge_idempotency_conflict"
    )


def test_openwebui_utility_task_creates_no_session_job_or_memory(bridge) -> None:
    client, agents, _, _, memories, notifier, database = bridge
    response = client.post(
        "/v1/chat/completions",
        headers=_headers(**{"X-OpenWebUI-Task": "title_generation"}),
        json=_body(stream=False),
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "utility_task_disabled"
    assert agents.list_sessions() == []
    assert memories.list() == []
    assert notifier.executions == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM chat_runs").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM external_chat_links").fetchone()[0]
            == 0
        )


def test_stream_disconnect_does_not_cancel_and_retry_replays(bridge) -> None:
    _, agents, runs, links, _, _, _ = bridge
    identity = openai_compat.BridgeIdentity(
        opaque_user_id="disconnect-user",
        external_chat_id="disconnect-chat",
        external_message_id="disconnect-response-message",
        external_user_message_id="disconnect-user-message",
        parent_external_message_id=None,
        task=None,
    )
    run = openai_compat._prepare_run(
        input_text="long answer",
        identity=identity,
        agent_service=agents,
        run_service=runs,
        link_service=links,
    )
    claimed = runs.claim("detached-worker", run_id=run.id)
    assert claimed is not None and claimed.attempt_id

    async def disconnect_then_finish() -> str:
        stream = openai_compat._stream_run(run.id, runs)
        first_event = await stream.__anext__()
        await stream.aclose()
        still_running = runs.get(run.id)
        assert still_running is not None
        assert still_running.status == ChatRunStatus.RUNNING
        runs.append_event(
            run.id,
            "token",
            {"content": "finished later"},
            partial_text="finished later",
            attempt_id=claimed.attempt_id,
        )
        runs.complete(run.id, attempt_id=claimed.attempt_id)
        replay = openai_compat._stream_run(run.id, runs)
        return first_event + "".join([event async for event in replay])

    output = asyncio.run(disconnect_then_finish())
    assert "finished later" in output
    assert output.endswith("data: [DONE]\n\n")


def test_sse_chunks_are_valid_json_documents(bridge) -> None:
    response = bridge[0].post(
        "/v1/chat/completions",
        headers=_headers(),
        json=_body(stream=True),
    )
    documents = []
    for line in response.text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            documents.append(json.loads(line.removeprefix("data: ")))
    assert documents[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert documents[-1]["choices"][0]["finish_reason"] == "stop"


def test_bridge_worker_retrieves_and_writes_stable_shared_memory(
    bridge, monkeypatch
) -> None:
    _, agents, runs, links, memories, _, _ = bridge
    memories.create(
        MemoryCreate(
            scope=MemoryScope.GLOBAL,
            kind=MemoryKind.FACT,
            content="Use evidence-first answers for evidence requests.",
            source="user",
        )
    )
    identity = openai_compat.BridgeIdentity(
        opaque_user_id="memory-user",
        external_chat_id="memory-chat",
        external_message_id="memory-response-message",
        external_user_message_id="memory-user-message",
        parent_external_message_id=None,
        task=None,
    )
    run = openai_compat._prepare_run(
        input_text="I prefer concise comparison tables for evidence answers.",
        identity=identity,
        agent_service=agents,
        run_service=runs,
        link_service=links,
    )
    ollama = CapturingOllama()
    worker = AgentChatWorker(settings=get_settings(), ollama=ollama)

    async def fake_knowledge_context(session, query: str) -> str:
        assert query == run.input_text
        return "[source=test; chunk=1] retrieved local knowledge"

    monkeypatch.setattr(worker, "_knowledge_context", fake_knowledge_context)
    claimed = worker.run_service.claim("bridge-memory-worker", run_id=run.id)
    assert claimed is not None
    asyncio.run(worker._execute(claimed))

    assert "retrieved local knowledge" in ollama.system_prompt
    assert "Use evidence-first answers" in ollama.system_prompt
    preferences = memories.search(
        "prefer concise comparison tables",
        kinds=[MemoryKind.PREFERENCE],
    )
    episodes = memories.search("shared result", kinds=[MemoryKind.EPISODE])
    assert preferences[0].memory.source == "open-webui"
    assert episodes == []


def test_bridge_memory_suppression_field_header_and_temporary_chat(bridge) -> None:
    client, agents, runs, links, _, notifier, _ = bridge

    field_headers = _headers(
        **{
            "X-OpenWebUI-Chat-Id": "field-chat",
            "X-OpenWebUI-Message-Id": "field-response",
            "X-OpenWebUI-User-Message-Id": "field-user-message",
        }
    )
    field_response = client.post(
        "/v1/chat/completions",
        headers=field_headers,
        json=_body(stream=False, suppress_memory=True),
    )
    assert field_response.status_code == 200
    field_link = links.get_message(
        source="open-webui",
        opaque_user_id="opaque-user-1",
        external_chat_id="field-chat",
        external_message_id="field-response",
    )
    assert field_link is not None
    assert runs.get(field_link.job_id).metadata["suppress_memory"] is True

    changed_privacy = client.post(
        "/v1/chat/completions",
        headers=field_headers,
        json=_body(stream=False, suppress_memory=False),
    )
    assert changed_privacy.status_code == 409

    header_headers = _headers(
        **{
            "X-OpenWebUI-Chat-Id": "header-chat",
            "X-OpenWebUI-Message-Id": "header-response",
            "X-OpenWebUI-User-Message-Id": "header-user-message",
            "X-Personal-Agent-Suppress-Memory": "true",
        }
    )
    assert (
        client.post(
            "/v1/chat/completions",
            headers=header_headers,
            json=_body(stream=False),
        ).status_code
        == 200
    )
    header_link = links.get_message(
        source="open-webui",
        opaque_user_id="opaque-user-1",
        external_chat_id="header-chat",
        external_message_id="header-response",
    )
    assert header_link is not None
    assert runs.get(header_link.job_id).metadata["suppress_memory"] is True

    temporary_headers = _headers(
        **{
            "X-OpenWebUI-Chat-Id": "",
            "X-OpenWebUI-Message-Id": "temporary-response",
            "X-OpenWebUI-User-Message-Id": "temporary-user-message",
        }
    )
    temporary_response = client.post(
        "/v1/chat/completions",
        headers=temporary_headers,
        json=_body(stream=False),
    )
    assert temporary_response.status_code == 200
    temporary_runs = [
        item
        for item in runs.list_runs()
        if item.metadata.get("temporary_chat") is True
    ]
    assert len(temporary_runs) == 1
    assert temporary_runs[0].metadata["suppress_memory"] is True

    saved_headers = _headers(
        **{
            "X-OpenWebUI-Chat-Id": "first-saved-chat",
            "X-OpenWebUI-Message-Id": "saved-response",
            "X-OpenWebUI-User-Message-Id": "saved-user-message",
        }
    )
    saved_response = client.post(
        "/v1/chat/completions",
        headers=saved_headers,
        json=_body(stream=False),
    )
    assert saved_response.status_code == 200
    saved_link = links.get_message(
        source="open-webui",
        opaque_user_id="opaque-user-1",
        external_chat_id="first-saved-chat",
        external_message_id="saved-response",
    )
    assert saved_link is not None
    assert runs.get(saved_link.job_id).metadata["suppress_memory"] is False
    assert notifier.executions == 4
    assert len(agents.list_sessions()) == 4


def test_regeneration_creates_new_job_without_duplicate_user_message(
    bridge, monkeypatch
) -> None:
    _, agents, runs, links, _, _, _ = bridge
    worker = AgentChatWorker(settings=get_settings(), ollama=CapturingOllama())

    async def no_knowledge(_session, _query: str) -> str:
        return ""

    monkeypatch.setattr(worker, "_knowledge_context", no_knowledge)
    first_identity = openai_compat.BridgeIdentity(
        opaque_user_id="regen-user",
        external_chat_id="regen-chat",
        external_message_id="response-version-1",
        external_user_message_id="same-user-turn",
        parent_external_message_id="previous-response",
        task=None,
    )
    second_identity = openai_compat.BridgeIdentity(
        opaque_user_id="regen-user",
        external_chat_id="regen-chat",
        external_message_id="response-version-2",
        external_user_message_id="same-user-turn",
        parent_external_message_id="previous-response",
        task=None,
    )
    first = openai_compat._prepare_run(
        input_text="Regenerate this answer",
        identity=first_identity,
        agent_service=agents,
        run_service=runs,
        link_service=links,
    )
    claimed = worker.run_service.claim("regen-worker", run_id=first.id)
    assert claimed is not None
    asyncio.run(worker._execute(claimed))

    second = openai_compat._prepare_run(
        input_text="Regenerate this answer",
        identity=second_identity,
        agent_service=agents,
        run_service=runs,
        link_service=links,
    )
    claimed = worker.run_service.claim("regen-worker", run_id=second.id)
    assert claimed is not None
    asyncio.run(worker._execute(claimed))

    assert first.id != second.id
    session = agents.list_sessions()[0]
    assert [message.role for message in session.messages] == [
        "user",
        "assistant",
        "assistant",
    ]
    user_message_ids = {
        message.id for message in session.messages if message.role == "user"
    }
    assert len(user_message_ids) == 1
