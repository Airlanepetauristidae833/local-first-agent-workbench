import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.schemas.chat_run import ChatRunStatus
from app.services.chat_run_service import (
    ChatRunConflictError,
    ChatRunService,
)


def test_create_is_idempotent_and_uses_independent_tables(tmp_path) -> None:
    database = tmp_path / "personal-agent.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE agent_sessions (id TEXT PRIMARY KEY, document TEXT)"
        )
        connection.execute(
            "INSERT INTO agent_sessions VALUES ('existing', 'preserved')"
        )

    service = ChatRunService(database)
    created = service.create(
        "session-1",
        "Explain the evidence",
        request_message_id="message-1",
        metadata={"project": "paper"},
        idempotency_key="request-1",
    )
    duplicate = service.create(
        "session-1",
        "Explain the evidence",
        request_message_id="message-1",
        metadata={"project": "paper"},
        idempotency_key="request-1",
    )

    assert duplicate.id == created.id
    assert created.status == ChatRunStatus.QUEUED
    assert created.last_event_seq == 1
    assert [event.event_type for event in service.list_events(created.id)] == [
        "run_created"
    ]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT document FROM agent_sessions WHERE id = 'existing'"
        ).fetchone()[0] == "preserved"

    with pytest.raises(ChatRunConflictError, match="different input"):
        service.create(
            "session-1",
            "A different prompt",
            idempotency_key="request-1",
        )


def test_concurrent_claim_has_exactly_one_winner(tmp_path) -> None:
    service = ChatRunService(tmp_path / "agent.sqlite3")
    created = service.create("session-1", "Start a durable response")

    def claim(worker_id: str):
        try:
            return service.claim(worker_id, run_id=created.id)
        except ChatRunConflictError:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(claim, [f"worker-{index}" for index in range(8)]))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].status == ChatRunStatus.RUNNING
    assert winners[0].attempt_id
    assert winners[0].attempt_no == 1
    assert [event.seq for event in service.list_events(created.id)] == [1, 2]


def test_partial_events_are_atomic_idempotent_and_replayable(tmp_path) -> None:
    service = ChatRunService(tmp_path / "agent.sqlite3")
    created = service.create("session-1", "Stream a response")
    claimed = service.claim("worker-1", run_id=created.id)
    assert claimed is not None and claimed.attempt_id is not None

    chunks = [f"chunk-{index};" for index in range(20)]

    def append(item: tuple[int, str]):
        index, chunk = item
        return service.append_event(
            created.id,
            "token",
            {"index": index},
            partial_text=chunk,
            attempt_id=claimed.attempt_id,
            idempotency_key=f"token-{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        events = list(executor.map(append, enumerate(chunks)))

    assert len({event.seq for event in events}) == len(chunks)
    replay = service.list_events(created.id)
    assert [event.seq for event in replay] == list(range(1, len(replay) + 1))
    replayed_text = "".join(
        event.payload.get("partial_text", "")
        for event in replay
        if event.event_type == "token"
    )
    assert service.get(created.id).partial_text == replayed_text
    assert sorted(event.payload["index"] for event in replay if event.event_type == "token") == list(
        range(len(chunks))
    )

    original = append((0, chunks[0]))
    assert original.seq == next(
        event.seq
        for event in replay
        if event.idempotency_key == "token-0"
    )
    assert service.get(created.id).partial_text == replayed_text
    with pytest.raises(ChatRunConflictError, match="different content"):
        service.append_event(
            created.id,
            "token",
            {"index": 999},
            partial_text="different",
            attempt_id=claimed.attempt_id,
            idempotency_key="token-0",
        )


def test_complete_and_fail_are_idempotent_and_reject_stale_attempts(tmp_path) -> None:
    service = ChatRunService(tmp_path / "agent.sqlite3")
    completed_run = service.create("session-1", "Complete me")
    claimed = service.claim("worker-1", run_id=completed_run.id)
    assert claimed is not None and claimed.attempt_id is not None
    service.append_event(
        completed_run.id,
        "token",
        partial_text="finished",
        attempt_id=claimed.attempt_id,
        idempotency_key="token-final",
    )
    completed = service.complete(
        completed_run.id,
        attempt_id=claimed.attempt_id,
        idempotency_key="complete-1",
    )
    duplicate = service.complete(
        completed_run.id,
        attempt_id=claimed.attempt_id,
        idempotency_key="complete-1",
    )
    assert duplicate == completed
    assert completed.status == ChatRunStatus.COMPLETED
    assert completed.final_text == "finished"
    assert service.list_events(completed.id)[-1].event_type == "run_completed"

    failed_run = service.create("session-1", "Fail me")
    failed_claim = service.claim("worker-2", run_id=failed_run.id)
    assert failed_claim is not None and failed_claim.attempt_id is not None
    with pytest.raises(ChatRunConflictError, match="stale"):
        service.fail(failed_run.id, "boom", attempt_id="stale-attempt")
    failed = service.fail(
        failed_run.id,
        "boom",
        attempt_id=failed_claim.attempt_id,
    )
    assert service.fail(
        failed_run.id,
        "boom",
        attempt_id=failed_claim.attempt_id,
    ) == failed
    assert failed.status == ChatRunStatus.FAILED
    assert failed.error == "boom"


def test_cancel_is_idempotent_for_queued_and_running_runs(tmp_path) -> None:
    service = ChatRunService(tmp_path / "agent.sqlite3")
    queued = service.create("session-1", "Cancel while queued")
    cancelled = service.cancel(queued.id, reason="user stopped")
    assert cancelled.status == ChatRunStatus.CANCELLED
    assert service.cancel(queued.id, reason="duplicate request") == cancelled

    running = service.create("session-1", "Cancel while running")
    claimed = service.claim("worker-1", run_id=running.id)
    assert claimed is not None
    cancelled_running = service.cancel(running.id)
    assert cancelled_running.status == ChatRunStatus.CANCELLED
    assert cancelled_running.partial_text == ""


def test_requeue_interrupted_resets_partial_text_and_invalidates_attempt(tmp_path) -> None:
    service = ChatRunService(tmp_path / "agent.sqlite3")
    first = service.create("session-1", "Recover me")
    second = service.create("session-2", "Recover me too")
    first_claim = service.claim("worker-a", run_id=first.id)
    second_claim = service.claim("worker-b", run_id=second.id)
    assert first_claim is not None and first_claim.attempt_id is not None
    assert second_claim is not None and second_claim.attempt_id is not None
    service.append_event(
        first.id,
        "token",
        partial_text="preserved",
        attempt_id=first_claim.attempt_id,
        idempotency_key="first-token",
    )

    requeued = service.requeue_interrupted()
    assert {run.id for run in requeued} == {first.id, second.id}
    recovered_first = service.get(first.id)
    assert recovered_first is not None
    assert recovered_first.status == ChatRunStatus.QUEUED
    assert recovered_first.partial_text == ""
    assert recovered_first.worker_id is None
    assert recovered_first.attempt_id is None
    assert service.requeue_interrupted() == []
    requeue_event = next(
        event
        for event in service.list_events(first.id)
        if event.event_type == "run_requeued"
    )
    assert requeue_event.payload["reset"] is True
    assert requeue_event.payload["previous_partial_length"] == len("preserved")
    assert "preserved" not in str(requeue_event.payload)

    reclaimed = service.claim("worker-c", run_id=first.id)
    assert reclaimed is not None and reclaimed.attempt_id is not None
    assert reclaimed.attempt_id != first_claim.attempt_id
    assert reclaimed.attempt_no == 2
    with pytest.raises(ChatRunConflictError, match="stale"):
        service.append_event(
            first.id,
            "token",
            partial_text="must-not-append",
            attempt_id=first_claim.attempt_id,
        )
    event_types = [event.event_type for event in service.list_events(first.id)]
    assert event_types == [
        "run_created",
        "run_claimed",
        "token",
        "run_requeued",
        "run_claimed",
    ]


def test_list_events_supports_cursor_replay(tmp_path) -> None:
    service = ChatRunService(tmp_path / "agent.sqlite3")
    run = service.create("session-1", "Replay me")
    claimed = service.claim("worker", run_id=run.id)
    assert claimed is not None and claimed.attempt_id is not None
    service.append_event(
        run.id,
        "progress",
        {"percent": 50},
        attempt_id=claimed.attempt_id,
    )
    service.complete(run.id, "done", attempt_id=claimed.attempt_id)

    first_page = service.list_events(run.id, limit=2)
    second_page = service.list_events(run.id, after_seq=first_page[-1].seq)
    assert [event.seq for event in first_page + second_page] == [1, 2, 3, 4]
