from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Annotated, Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.schemas.agent import (
    AgentPhase,
    AgentSession,
    AgentStatus,
    KnowledgeState,
)
from app.schemas.chat_run import ChatRun, ChatRunStatus
from app.schemas.openai_compat import (
    OpenAIChatCompletionRequest,
    OpenAIModel,
    OpenAIModelList,
)
from app.services.agent_chat_runner import notify_agent_chat_worker
from app.services.agent_service import (
    AgentConflictError,
    AgentDeletedError,
    AgentService,
)
from app.services.chat_run_service import (
    ChatRunConflictError,
    ChatRunService,
)
from app.services.external_chat_link_service import (
    ExternalChatLinkConflictError,
    ExternalChatLinkService,
)

router = APIRouter(prefix="/v1", tags=["openai-compatible-personal-agent"])

MODEL_ID = "personal-agent"
SOURCE = "open-webui"
BRIDGE_TOKEN_ENV = "PERSONAL_AGENT_BRIDGE_TOKEN"
_POLL_SECONDS = 0.1
_HEARTBEAT_SECONDS = 10.0
_NON_STREAM_TIMEOUT_SECONDS = 3_600.0
_MAX_EXTERNAL_ID_LENGTH = 2_000


@dataclass(frozen=True, slots=True)
class BridgeIdentity:
    opaque_user_id: str
    external_chat_id: str
    external_message_id: str
    external_user_message_id: str
    parent_external_message_id: str | None
    task: str | None
    temporary_chat: bool = False


def get_agent_service() -> AgentService:
    service = AgentService(get_settings().agent_store_path)
    service.initialize()
    return service


def get_chat_run_service() -> ChatRunService:
    service = ChatRunService(get_settings().agent_store_path)
    service.initialize()
    return service


def get_external_chat_link_service() -> ExternalChatLinkService:
    service = ExternalChatLinkService(get_settings().agent_store_path)
    service.initialize()
    return service


def get_worker_notifier() -> Callable[[], None]:
    return notify_agent_chat_worker


def require_bridge_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = os.getenv(BRIDGE_TOKEN_ENV, "").strip()
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "message": "The Personal Agent bridge is not configured.",
                    "type": "service_unavailable",
                    "code": "bridge_not_configured",
                }
            },
        )
    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not supplied
        or not hmac.compare_digest(supplied.strip(), expected)
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "message": "Invalid bridge credential.",
                    "type": "authentication_error",
                    "code": "invalid_bridge_token",
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_bridge_identity(
    opaque_user_id: Annotated[
        str | None, Header(alias="X-OpenWebUI-User-Id")
    ] = None,
    external_chat_id: Annotated[
        str | None, Header(alias="X-OpenWebUI-Chat-Id")
    ] = None,
    external_message_id: Annotated[
        str | None, Header(alias="X-OpenWebUI-Message-Id")
    ] = None,
    external_user_message_id: Annotated[
        str | None, Header(alias="X-OpenWebUI-User-Message-Id")
    ] = None,
    parent_external_message_id: Annotated[
        str | None, Header(alias="X-OpenWebUI-User-Message-Parent-Id")
    ] = None,
    task: Annotated[str | None, Header(alias="X-OpenWebUI-Task")] = None,
) -> BridgeIdentity:
    required = {
        "X-OpenWebUI-User-Id": opaque_user_id,
        "X-OpenWebUI-Message-Id": external_message_id,
        "X-OpenWebUI-User-Message-Id": external_user_message_id,
    }
    missing = [name for name, value in required.items() if not (value or "").strip()]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": "Missing stateful bridge headers: " + ", ".join(missing),
                    "type": "invalid_request_error",
                    "code": "missing_bridge_identity",
                }
            },
        )
    values = [
        opaque_user_id or "",
        external_chat_id or "",
        external_message_id or "",
        external_user_message_id or "",
        parent_external_message_id or "",
        task or "",
    ]
    if any(len(value) > _MAX_EXTERNAL_ID_LENGTH for value in values):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": "A bridge identifier is too long.",
                    "type": "invalid_request_error",
                    "code": "bridge_identifier_too_long",
                }
            },
        )
    return BridgeIdentity(
        opaque_user_id=(opaque_user_id or "").strip(),
        external_chat_id=(external_chat_id or "").strip(),
        external_message_id=(external_message_id or "").strip(),
        external_user_message_id=(external_user_message_id or "").strip(),
        parent_external_message_id=(parent_external_message_id or "").strip()
        or None,
        task=(task or "").strip() or None,
        temporary_chat=not (external_chat_id or "").strip(),
    )


@router.get("/models", response_model=OpenAIModelList)
def models(_: None = Depends(require_bridge_auth)) -> OpenAIModelList:
    return OpenAIModelList(data=[OpenAIModel(id=MODEL_ID)])


@router.post("/chat/completions")
async def chat_completions(
    request: OpenAIChatCompletionRequest,
    suppress_memory_header: Annotated[
        bool | None,
        Header(alias="X-Personal-Agent-Suppress-Memory"),
    ] = None,
    _: None = Depends(require_bridge_auth),
    identity: BridgeIdentity = Depends(get_bridge_identity),
    agent_service: AgentService = Depends(get_agent_service),
    run_service: ChatRunService = Depends(get_chat_run_service),
    link_service: ExternalChatLinkService = Depends(
        get_external_chat_link_service
    ),
    notify_worker: Callable[[], None] = Depends(get_worker_notifier),
):
    if request.model not in {MODEL_ID, f"agent.{MODEL_ID}"}:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "message": f"Model '{request.model}' was not found.",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            },
        )
    if identity.task:
        # Auxiliary title/tag/follow-up jobs are not user turns. Refusing them
        # is safer than polluting durable memory or triggering an Agent task.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": "Open WebUI utility tasks are disabled for the Personal Agent model.",
                    "type": "invalid_request_error",
                    "code": "utility_task_disabled",
                }
            },
        )

    input_text = _latest_user_text(request)
    try:
        run = _prepare_run(
            input_text=input_text,
            identity=identity,
            suppress_memory=(
                request.suppress_memory
                or bool(suppress_memory_header)
                or identity.temporary_chat
            ),
            agent_service=agent_service,
            run_service=run_service,
            link_service=link_service,
        )
    except (ExternalChatLinkConflictError, ChatRunConflictError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "message": str(exc),
                    "type": "conflict_error",
                    "code": "bridge_idempotency_conflict",
                }
            },
        ) from exc
    notify_worker()
    if request.stream:
        return StreamingResponse(
            _stream_run(run.id, run_service),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    terminal = await _wait_for_terminal(run.id, run_service)
    return _completion_document(terminal)


def _prepare_run(
    *,
    input_text: str,
    identity: BridgeIdentity,
    suppress_memory: bool = False,
    agent_service: AgentService,
    run_service: ChatRunService,
    link_service: ExternalChatLinkService,
) -> ChatRun:
    temporary_chat = identity.temporary_chat or not identity.external_chat_id
    effective_chat_id = identity.external_chat_id or (
        "temporary:" + identity.external_user_message_id
    )
    user_hash = link_service.identifier_hash(SOURCE, identity.opaque_user_id)
    chat_hash = link_service.identifier_hash(SOURCE, effective_chat_id)
    message_hash = link_service.identifier_hash(
        SOURCE, identity.external_message_id
    )
    user_message_hash = link_service.identifier_hash(
        SOURCE, identity.external_user_message_id
    )
    parent_hash = (
        link_service.identifier_hash(SOURCE, identity.parent_external_message_id)
        if identity.parent_external_message_id
        else ""
    )
    request_hash = _request_hash(input_text, user_message_hash, parent_hash)

    chat_link = link_service.get_chat(
        source=SOURCE,
        opaque_user_id=identity.opaque_user_id,
        external_chat_id=effective_chat_id,
    )
    if chat_link is None:
        session_id = _stable_uuid("session", user_hash, chat_hash)
        session = agent_service.get_session(session_id)
        if session is None:
            session = _new_bridge_session(session_id, chat_hash)
            try:
                agent_service.save(session)
            except (sqlite3.IntegrityError, AgentConflictError):
                session = agent_service.get_session(session_id)
                if session is None:
                    raise
            except AgentDeletedError as exc:
                raise ExternalChatLinkConflictError(
                    "the linked Personal Agent session was deleted"
                ) from exc
        chat_link = link_service.ensure_chat(
            source=SOURCE,
            opaque_user_id=identity.opaque_user_id,
            external_chat_id=effective_chat_id,
            agent_session_id=session_id,
        )
    session = agent_service.get_session(chat_link.agent_session_id)
    if session is None:
        raise ExternalChatLinkConflictError(
            "the linked Personal Agent session no longer exists"
        )
    if session.archived_at is not None:
        raise ExternalChatLinkConflictError(
            "the linked Personal Agent session is archived"
        )

    agent_message_id = _stable_uuid(
        "message", user_hash, chat_hash, user_message_hash
    )
    job_id = _stable_uuid("job", user_hash, chat_hash, message_hash)
    metadata = {
        "source": SOURCE,
        "external_user_hash": user_hash,
        "external_chat_hash": chat_hash,
        "external_message_hash": message_hash,
        "external_user_message_hash": user_message_hash,
        "parent_external_message_hash": parent_hash or None,
        "suppress_memory": suppress_memory or temporary_chat,
    }
    if temporary_chat:
        metadata["temporary_chat"] = True
    run = run_service.create(
        chat_link.agent_session_id,
        input_text,
        request_message_id=agent_message_id,
        metadata=metadata,
        idempotency_key=f"{SOURCE}:{user_hash}:{chat_hash}:{message_hash}",
        run_id=job_id,
    )
    message_link = link_service.ensure_message(
        source=SOURCE,
        opaque_user_id=identity.opaque_user_id,
        external_chat_id=effective_chat_id,
        external_message_id=identity.external_message_id,
        parent_external_message_id=identity.parent_external_message_id,
        agent_message_id=agent_message_id,
        job_id=job_id,
        request_hash=request_hash,
    )
    if message_link.job_id != run.id:
        raise ExternalChatLinkConflictError(
            "the external message is linked to another execution job"
        )
    return run


def _new_bridge_session(session_id: str, chat_hash: str) -> AgentSession:
    now = datetime.now(timezone.utc)
    return AgentSession(
        id=session_id,
        title=f"Open WebUI bridge {chat_hash[:10]}",
        goal="Private Personal Agent bridge conversation.",
        internal=True,
        phase=AgentPhase.IMPLEMENTATION,
        status=AgentStatus.WAITING_FOR_STAGE,
        execution_mode="local",
        local_percent=100,
        codex_percent=0,
        routing_reason=(
            "This anonymous bridge session uses the local Personal Agent, "
            "durable memory, and local knowledge retrieval."
        ),
        knowledge_state=KnowledgeState.AVAILABLE,
        created_at=now,
        updated_at=now,
    )


def _latest_user_text(request: OpenAIChatCompletionRequest) -> str:
    message = next(
        (item for item in reversed(request.messages) if item.role == "user"),
        None,
    )
    if message is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": "messages must include a user message.",
                    "type": "invalid_request_error",
                    "code": "missing_user_message",
                }
            },
        )
    content = message.content
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts: list[str] = []
        unsupported = False
        for part in content:
            if not isinstance(part, dict):
                unsupported = True
                continue
            part_type = str(part.get("type") or "")
            value = part.get("text")
            if part_type in {"text", "input_text"} and isinstance(value, str):
                parts.append(value)
            else:
                unsupported = True
        if unsupported:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": {
                        "message": "The Personal Agent bridge currently accepts text content only.",
                        "type": "invalid_request_error",
                        "code": "unsupported_message_content",
                    }
                },
            )
        text = "\n".join(parts).strip()
    else:
        text = ""
    if not text:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "message": "The latest user message must contain text.",
                    "type": "invalid_request_error",
                    "code": "blank_user_message",
                }
            },
        )
    if len(text) > 100_000:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "error": {
                    "message": "The latest user message is too large.",
                    "type": "invalid_request_error",
                    "code": "message_too_large",
                }
            },
        )
    return text


async def _wait_for_terminal(
    run_id: str,
    service: ChatRunService,
    *,
    timeout_seconds: float = _NON_STREAM_TIMEOUT_SECONDS,
) -> ChatRun:
    deadline = monotonic() + timeout_seconds
    while True:
        run = service.get(run_id)
        if run is None:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error": {
                        "message": "The durable Agent job disappeared.",
                        "type": "server_error",
                        "code": "agent_job_missing",
                    }
                },
            )
        if run.status == ChatRunStatus.COMPLETED:
            return run
        if run.status == ChatRunStatus.FAILED:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error": {
                        "message": run.error or "The Personal Agent job failed.",
                        "type": "server_error",
                        "code": "agent_job_failed",
                    }
                },
            )
        if run.status == ChatRunStatus.CANCELLED:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": {
                        "message": run.error or "The Personal Agent job was cancelled.",
                        "type": "conflict_error",
                        "code": "agent_job_cancelled",
                    }
                },
            )
        if monotonic() >= deadline:
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT,
                detail={
                    "error": {
                        "message": "The durable job is still running; retry the same message to reattach.",
                        "type": "server_error",
                        "code": "agent_job_still_running",
                    }
                },
            )
        await asyncio.sleep(_POLL_SECONDS)


async def _stream_run(
    run_id: str,
    service: ChatRunService,
) -> AsyncIterator[str]:
    run = service.get(run_id)
    if run is None:
        yield _sse_error("The durable Agent job disappeared.", "agent_job_missing")
        yield "data: [DONE]\n\n"
        return
    created = int(run.created_at.timestamp())
    yield _sse_chunk(run, created, {"role": "assistant"})
    sent_text = run.partial_text
    after_seq = run.last_event_seq
    if sent_text:
        yield _sse_chunk(run, created, {"content": sent_text})
    last_transport_activity = monotonic()

    while True:
        events = service.list_events(run_id, after_seq=after_seq, limit=1_000)
        requeued = False
        for event in events:
            after_seq = event.seq
            if event.event_type == "run_requeued" and event.payload.get("reset"):
                requeued = True
                break
            if event.event_type != "token":
                continue
            content = str(event.payload.get("content") or "")
            if content:
                sent_text += content
                yield _sse_chunk(run, created, {"content": content})
                last_transport_activity = monotonic()
        if requeued:
            yield _sse_error(
                "The Agent worker restarted; retry the same message to replay cleanly.",
                "agent_job_requeued",
            )
            yield "data: [DONE]\n\n"
            return

        current = service.get(run_id)
        if current is None:
            yield _sse_error("The durable Agent job disappeared.", "agent_job_missing")
            yield "data: [DONE]\n\n"
            return
        if current.status == ChatRunStatus.COMPLETED:
            final_text = current.final_text or current.partial_text
            if final_text != sent_text:
                if final_text.startswith(sent_text):
                    remainder = final_text[len(sent_text) :]
                    if remainder:
                        yield _sse_chunk(current, created, {"content": remainder})
                else:
                    yield _sse_error(
                        "Persisted Agent output changed during replay.",
                        "agent_replay_conflict",
                    )
                    yield "data: [DONE]\n\n"
                    return
            yield _sse_chunk(current, created, {}, finish_reason="stop")
            yield "data: [DONE]\n\n"
            return
        if current.status == ChatRunStatus.FAILED:
            yield _sse_error(
                current.error or "The Personal Agent job failed.",
                "agent_job_failed",
            )
            yield "data: [DONE]\n\n"
            return
        if current.status == ChatRunStatus.CANCELLED:
            yield _sse_error(
                current.error or "The Personal Agent job was cancelled.",
                "agent_job_cancelled",
            )
            yield "data: [DONE]\n\n"
            return
        if monotonic() - last_transport_activity >= _HEARTBEAT_SECONDS:
            yield ": keep-alive\n\n"
            last_transport_activity = monotonic()
        await asyncio.sleep(_POLL_SECONDS)


def _completion_document(run: ChatRun) -> dict[str, Any]:
    content = run.final_text or run.partial_text
    return {
        "id": f"chatcmpl-{run.id}",
        "object": "chat.completion",
        "created": int(run.created_at.timestamp()),
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def _sse_chunk(
    run: ChatRun,
    created: int,
    delta: dict[str, Any],
    *,
    finish_reason: str | None = None,
) -> str:
    document = {
        "id": f"chatcmpl-{run.id}",
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return "data: " + json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    ) + "\n\n"


def _sse_error(message: str, code: str) -> str:
    document = {
        "error": {
            "message": message,
            "type": "server_error",
            "code": code,
        }
    }
    return "data: " + json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    ) + "\n\n"


def _request_hash(
    input_text: str,
    user_message_hash: str,
    parent_hash: str,
) -> str:
    document = json.dumps(
        {
            "model": MODEL_ID,
            "input": input_text,
            "user_message": user_message_hash,
            "parent": parent_hash,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _stable_uuid(kind: str, *parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, "ai-workstation:" + kind + ":" + ":".join(parts)))
