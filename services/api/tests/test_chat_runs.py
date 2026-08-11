from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.config import get_settings
from app.schemas.agent import (
    AgentPhase,
    AgentSession,
    AgentStatus,
    KnowledgeState,
)
from app.schemas.chat_run import ChatRunStatus
from app.services.agent_service import AgentService
from app.services.chat_run_service import ChatRunService
from app.services.memory_service import MemoryService


def _session(
    session_id: str = "durable-session",
    project_id: str | None = None,
) -> AgentSession:
    now = datetime.now(timezone.utc)
    return AgentSession(
        id=session_id,
        title="Durable route test",
        goal="Return a durable local-model response",
        project_id=project_id,
        phase=AgentPhase.IMPLEMENTATION,
        status=AgentStatus.WAITING_FOR_STAGE,
        execution_mode="local",
        local_percent=100,
        codex_percent=0,
        routing_reason="The local model can answer this conversation.",
        knowledge_state=KnowledgeState.AVAILABLE,
        created_at=now,
        updated_at=now,
    )


def _agent_service() -> AgentService:
    return AgentService(get_settings().agent_store_path)


def _disable_worker_network(client) -> None:
    worker = client.app.state.agent_chat_worker

    async def no_knowledge(_session, _query):
        return ""

    worker._knowledge_context = no_knowledge


def _wait_for_terminal(client, run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/agent/chat-runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["status"] in {"completed", "failed", "cancelled"}:
            return run
        time.sleep(0.02)
    raise AssertionError("chat run did not reach a terminal state")


def _sse_events(body: str) -> list[dict]:
    events: list[dict] = []
    for frame in body.split("\n\n"):
        if not frame or frame.startswith(":"):
            continue
        lines = frame.splitlines()
        event_id = int(next(line[4:] for line in lines if line.startswith("id: ")))
        event_type = next(
            line[7:] for line in lines if line.startswith("event: ")
        )
        data = json.loads(
            next(line[6:] for line in lines if line.startswith("data: "))
        )
        events.append({"id": event_id, "type": event_type, "data": data})
    return events


def test_chat_run_route_is_concurrent_and_idempotent(client) -> None:
    _disable_worker_network(client)
    _agent_service().save(_session())

    def create(_index: int):
        return client.post(
            "/api/v1/agent/sessions/durable-session/chat-runs",
            json={"content": "Write the durable answer."},
            headers={"Idempotency-Key": "same-request"},
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        responses = list(executor.map(create, range(16)))

    assert {response.status_code for response in responses} == {202}
    run_ids = {response.json()["id"] for response in responses}
    assert len(run_ids) == 1
    run_id = run_ids.pop()
    completed = _wait_for_terminal(client, run_id)
    assert completed["status"] == "completed"
    assert completed["final_text"] == "\u76d0\u57ce"

    session = _agent_service().get_session("durable-session")
    assert session is not None
    assert session.active_operation is None
    run_messages = [
        message
        for message in session.messages
        if message.metadata.get("chat_run_id") == run_id
    ]
    assert [message.role for message in run_messages] == ["user", "assistant"]

    replay = client.post(
        "/api/v1/agent/sessions/durable-session/chat-runs",
        json={"content": "Write the durable answer."},
        headers={"Idempotency-Key": "same-request"},
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == run_id
    replayed_session = _agent_service().get_session("durable-session")
    assert replayed_session is not None
    assert replayed_session.active_operation is None


def test_sse_replays_persisted_events_and_resumes_from_last_event_id(client) -> None:
    _disable_worker_network(client)
    _agent_service().save(_session("sse-session"))
    created = client.post(
        "/api/v1/agent/sessions/sse-session/chat-runs",
        json={"content": "Stream and persist this answer."},
        headers={"Idempotency-Key": "sse-request"},
    )
    assert created.status_code == 202
    run_id = created.json()["id"]
    completed = _wait_for_terminal(client, run_id)
    assert completed["status"] == "completed"

    replay = client.get(f"/api/v1/agent/chat-runs/{run_id}/events")
    assert replay.status_code == 200
    assert replay.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(replay.text)
    assert [event["id"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert events[0]["type"] == "run_created"
    assert events[-1]["type"] == "run_completed"
    token_text = "".join(
        event["data"]["payload"].get("content", "")
        for event in events
        if event["type"] == "token"
    )
    assert token_text == completed["final_text"]

    cursor = events[-3]["id"]
    resumed = client.get(
        f"/api/v1/agent/chat-runs/{run_id}/events",
        headers={"Last-Event-ID": str(cursor)},
    )
    resumed_events = _sse_events(resumed.text)
    assert resumed_events
    assert all(event["id"] > cursor for event in resumed_events)
    assert resumed_events[-1]["type"] == "run_completed"
    invalid = client.get(
        f"/api/v1/agent/chat-runs/{run_id}/events",
        headers={"Last-Event-ID": "not-a-sequence"},
    )
    assert invalid.status_code == 400


def test_chat_run_can_suppress_memory_and_privacy_is_idempotent(client) -> None:
    _disable_worker_network(client)
    _agent_service().save(_session("private-run", project_id="paper-project"))
    created = client.post(
        "/api/v1/agent/sessions/private-run/chat-runs",
        json={
            "content": "Keep this project conversation out of long-term memory.",
            "suppress_memory": True,
        },
        headers={"Idempotency-Key": "private-request"},
    )
    assert created.status_code == 202
    completed = _wait_for_terminal(client, created.json()["id"])
    assert completed["metadata"]["suppress_memory"] is True
    assert MemoryService(get_settings().agent_store_path).list() == []

    changed_privacy = client.post(
        "/api/v1/agent/sessions/private-run/chat-runs",
        json={
            "content": "Keep this project conversation out of long-term memory.",
            "suppress_memory": False,
        },
        headers={"Idempotency-Key": "private-request"},
    )
    assert changed_privacy.status_code == 409


def test_queued_cancel_releases_session_and_allows_another_run(
    client, monkeypatch
) -> None:
    worker = client.app.state.agent_chat_worker
    client.portal.call(worker.stop)
    monkeypatch.setattr(
        "app.routers.chat_runs.notify_agent_chat_worker", lambda: None
    )
    agent = _agent_service()
    agent.save(_session("cancel-session"))
    created = client.post(
        "/api/v1/agent/sessions/cancel-session/chat-runs",
        json={"content": "This queued answer will be cancelled."},
        headers={"Idempotency-Key": "cancel-request"},
    )
    assert created.status_code == 202
    run_id = created.json()["id"]
    assert created.json()["status"] == "queued"
    claimed_session = agent.get_session("cancel-session")
    assert claimed_session is not None
    assert claimed_session.active_operation == f"chat-run:{run_id}"

    cancelled = client.post(
        f"/api/v1/agent/chat-runs/{run_id}/cancel",
        json={"reason": "user changed direction"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    released = agent.get_session("cancel-session")
    assert released is not None and released.active_operation is None
    cancelled_messages = [
        message
        for message in released.messages
        if message.metadata.get("chat_run_id") == run_id
        and message.role == "assistant"
    ]
    assert len(cancelled_messages) == 1

    next_run = client.post(
        "/api/v1/agent/sessions/cancel-session/chat-runs",
        json={"content": "A new request is now allowed."},
        headers={"Idempotency-Key": "next-request"},
    )
    assert next_run.status_code == 202
    assert next_run.json()["id"] != run_id


def test_restart_sequence_requeues_run_and_preserves_valid_session_operation(
    tmp_path,
) -> None:
    database = tmp_path / "personal-agent.sqlite3"
    agent = AgentService(database)
    runs = ChatRunService(database)
    run = runs.create(
        "restart-session",
        "Resume after restart",
        request_message_id="restart-message",
        idempotency_key="restart-request",
    )
    claimed = runs.claim("old-worker", run_id=run.id)
    assert claimed is not None and claimed.attempt_id is not None
    runs.append_event(
        run.id,
        "token",
        {"content": "old partial"},
        partial_text="old partial",
        attempt_id=claimed.attempt_id,
    )
    session = _session("restart-session").model_copy(
        update={
            "active_operation": f"chat-run:{run.id}",
            "operation_started_at": datetime.now(timezone.utc),
        }
    )
    session = agent.append_message(
        session,
        "user",
        "chat",
        run.input_text,
        metadata={"chat_run_id": run.id},
        message_id=run.request_message_id,
    )
    agent.save(session)

    requeued = runs.requeue_interrupted(reason="API process restarted")
    valid_ids = {
        item.id
        for item in runs.list_runs(
            statuses={ChatRunStatus.QUEUED, ChatRunStatus.RUNNING}
        )
    }
    recovered = agent.recover_interrupted(set(), valid_ids)

    assert [item.id for item in requeued] == [run.id]
    assert recovered == []
    durable_run = runs.get(run.id)
    durable_session = agent.get_session("restart-session")
    assert durable_run is not None
    assert durable_run.status == ChatRunStatus.QUEUED
    assert durable_run.partial_text == ""
    assert durable_session is not None
    assert durable_session.active_operation == f"chat-run:{run.id}"
    assert len(
        [
            message
            for message in durable_session.messages
            if message.metadata.get("chat_run_id") == run.id
            and message.role == "user"
        ]
    ) == 1
