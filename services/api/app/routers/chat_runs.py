from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from functools import wraps
from threading import Lock
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.routers.agent import get_agent_service
from app.schemas.agent import AgentSession, AgentStatus, KnowledgeState
from app.schemas.chat_run import (
    ChatRun,
    ChatRunCancel,
    ChatRunCreate,
    ChatRunEvent,
    ChatRunStatus,
)
from app.services.agent_chat_runner import notify_agent_chat_worker
from app.services.agent_service import (
    AgentConflictError,
    AgentDeletedError,
    AgentService,
)
from app.services.chat_run_service import (
    ChatRunConflictError,
    ChatRunNotFoundError,
    ChatRunService,
)

router = APIRouter(prefix="/api/v1/agent", tags=["durable-chat"])
TERMINAL_STATUSES = {
    ChatRunStatus.COMPLETED,
    ChatRunStatus.FAILED,
    ChatRunStatus.CANCELLED,
}
_CHAT_RUN_CREATION_LOCK = Lock()


def _serialize_chat_run_creation(
    handler: Callable[..., ChatRun],
) -> Callable[..., ChatRun]:
    """Serialize the cross-service session-claim and run-create critical section.

    Agent sessions and chat runs share SQLite but are intentionally managed by
    separate services. The supported deployment has one API process, so this
    short coordinator lock ensures concurrent retries observe the first durable
    run before they can mutate the session a second time.
    """

    @wraps(handler)
    def wrapped(*args: Any, **kwargs: Any) -> ChatRun:
        with _CHAT_RUN_CREATION_LOCK:
            return handler(*args, **kwargs)

    return wrapped


def get_chat_run_service() -> ChatRunService:
    service = ChatRunService(get_settings().agent_store_path)
    service.initialize()
    return service


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ChatRunNotFoundError):
        return HTTPException(404, str(exc))
    return HTTPException(409, str(exc))


@router.post(
    "/sessions/{session_id}/chat-runs",
    response_model=ChatRun,
    status_code=status.HTTP_202_ACCEPTED,
)
@_serialize_chat_run_creation
def create_chat_run(
    session_id: str,
    payload: ChatRunCreate,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=200,
    ),
    chat_runs: ChatRunService = Depends(get_chat_run_service),
    agents: AgentService = Depends(get_agent_service),
) -> ChatRun:
    request_key = idempotency_key or str(uuid4())
    run_id = str(
        uuid5(NAMESPACE_URL, f"agent-chat-run:{session_id}:{request_key}")
    )
    request_message_id = str(
        uuid5(NAMESPACE_URL, f"agent-chat-message:{session_id}:{request_key}")
    )
    existing = chat_runs.get(run_id)
    if existing is not None:
        if (
            existing.session_id != session_id
            or existing.input_text != payload.content
            or existing.request_message_id != request_message_id
            or existing.idempotency_key != request_key
            or bool(existing.metadata.get("suppress_memory"))
            != payload.suppress_memory
        ):
            raise HTTPException(
                409,
                "The idempotency key was reused with different chat input.",
            )
        notify_agent_chat_worker()
        return existing
    operation_id = f"chat-run:{run_id}"

    def claim_session(current: AgentSession) -> AgentSession:
        if current.archived_at is not None:
            raise ChatRunConflictError(
                "archived tasks cannot start a chat run"
            )
        if current.active_operation not in {None, operation_id}:
            raise ChatRunConflictError(
                "the project already has a running operation"
            )
        if (
            current.active_operation != operation_id
            and (
                current.knowledge_state == KnowledgeState.BUILDING
                or current.status
                in {AgentStatus.LOCAL_RUNNING, AgentStatus.CODEX_RUNNING}
            )
        ):
            raise ChatRunConflictError(
                "the project already has a running operation"
            )
        updated = current
        if not any(
            message.id == request_message_id for message in current.messages
        ):
            updated = agents.append_message(
                updated,
                "user",
                "chat",
                payload.content,
                metadata={"chat_run_id": run_id},
                message_id=request_message_id,
            )
        return updated.model_copy(
            update={
                "active_operation": operation_id,
                "operation_started_at": datetime.now(timezone.utc),
            }
        )

    try:
        agents.mutate(session_id, claim_session)
    except AgentDeletedError as exc:
        raise HTTPException(404, "Agent project session does not exist.") from exc
    except (AgentConflictError, ChatRunConflictError) as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        run = chat_runs.create(
            session_id,
            payload.content,
            request_message_id=request_message_id,
            metadata={
                "source": "workbench",
                "suppress_memory": payload.suppress_memory,
            },
            idempotency_key=request_key,
            run_id=run_id,
        )
    except ChatRunConflictError as exc:
        try:
            agents.mutate(
                session_id,
                lambda current: current.model_copy(
                    update={
                        "active_operation": None,
                        "operation_started_at": None,
                    }
                )
                if current.active_operation == operation_id
                else current,
            )
        except (AgentConflictError, AgentDeletedError):
            pass
        raise _http_error(exc) from exc
    notify_agent_chat_worker()
    return run


@router.get("/chat-runs/{run_id}", response_model=ChatRun)
def get_chat_run(
    run_id: str,
    chat_runs: ChatRunService = Depends(get_chat_run_service),
) -> ChatRun:
    run = chat_runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Chat run does not exist.")
    return run


@router.post("/chat-runs/{run_id}/cancel", response_model=ChatRun)
def cancel_chat_run(
    run_id: str,
    payload: ChatRunCancel,
    chat_runs: ChatRunService = Depends(get_chat_run_service),
    agents: AgentService = Depends(get_agent_service),
) -> ChatRun:
    try:
        run = chat_runs.cancel(run_id, reason=payload.reason)
    except (ChatRunConflictError, ChatRunNotFoundError) as exc:
        raise _http_error(exc) from exc
    operation_id = f"chat-run:{run.id}"

    def release_session(current: AgentSession) -> AgentSession:
        if current.active_operation != operation_id:
            return current
        updated = current.model_copy(
            update={
                "active_operation": None,
                "operation_started_at": None,
            }
        )
        if any(
            message.role == "assistant"
            and message.metadata.get("chat_run_id") == run.id
            for message in updated.messages
        ):
            return updated
        return agents.append_message(
            updated,
            "assistant",
            "cancelled",
            "The background response was cancelled.",
            metadata={"chat_run_id": run.id},
            message_id=f"{run.id}:cancelled",
        )

    try:
        agents.mutate(run.session_id, release_session)
    except (AgentConflictError, AgentDeletedError):
        pass
    notify_agent_chat_worker()
    return run


def _event_frame(event: ChatRunEvent) -> str:
    data = json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"id: {event.seq}\n"
        f"event: {event.event_type}\n"
        f"data: {data}\n\n"
    )


@router.get("/chat-runs/{run_id}/events")
async def stream_chat_run_events(
    run_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    chat_runs: ChatRunService = Depends(get_chat_run_service),
) -> StreamingResponse:
    run = chat_runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Chat run does not exist.")
    cursor = after_seq
    if last_event_id not in {None, ""}:
        try:
            header_cursor = int(last_event_id)
        except ValueError as exc:
            raise HTTPException(
                400,
                "Last-Event-ID must be a non-negative integer.",
            ) from exc
        if header_cursor < 0:
            raise HTTPException(
                400,
                "Last-Event-ID must be a non-negative integer.",
            )
        cursor = max(cursor, header_cursor)

    async def replay():
        current_seq = cursor
        idle_polls = 0
        while True:
            events = await asyncio.to_thread(
                chat_runs.list_events,
                run_id,
                after_seq=current_seq,
                limit=500,
            )
            if events:
                idle_polls = 0
                for event in events:
                    current_seq = event.seq
                    yield _event_frame(event)
                continue
            latest = await asyncio.to_thread(chat_runs.get, run_id)
            if latest is None or latest.status in TERMINAL_STATUSES:
                return
            if await request.is_disconnected():
                return
            idle_polls += 1
            if idle_polls * 0.2 >= 10:
                idle_polls = 0
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(
        replay(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )
