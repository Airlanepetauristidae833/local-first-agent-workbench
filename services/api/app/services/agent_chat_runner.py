from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import datetime, timezone
from time import monotonic

import httpx

from app.config import Settings, get_settings
from app.schemas.agent import AgentPhase, AgentSession
from app.schemas.chat_run import ChatRun, ChatRunStatus
from app.schemas.memory import MemoryKind, MemoryRecord, MemoryScope
from app.services.agent_service import (
    AgentConflictError,
    AgentDeletedError,
    AgentService,
)
from app.services.chat_run_service import (
    ChatRunConflictError,
    ChatRunService,
)
from app.services.context_manager import (
    ContextPolicy,
    build_context_envelope,
    plan_compaction,
    trim_to_tokens,
)
from app.services.memory_extractor import (
    episode_memory,
    explicit_memory_candidates,
)
from app.services.memory_service import MemoryService
from app.services.ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaTimeoutError,
    get_ollama_client,
)

_GLOBAL_RETRIEVAL_KINDS = (
    MemoryKind.PREFERENCE,
    MemoryKind.CONSTRAINT,
    MemoryKind.FACT,
)


class _RunCancelled(RuntimeError):
    pass


class AgentChatWorker:
    """Single durable worker for long local-model conversations.

    Generation is detached from the browser request. Tokens are committed to a
    replayable event log, so an SSE client can disconnect and resume from its
    last event sequence without cancelling Ollama.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        ollama: OllamaClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.agent_service = AgentService(self.settings.agent_store_path)
        self.run_service = ChatRunService(self.settings.agent_store_path)
        self.memory_service = MemoryService(self.settings.agent_store_path)
        self.ollama = ollama or get_ollama_client()
        self.worker_id = f"agent-chat-{socket.gethostname()}"
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def policy(self) -> ContextPolicy:
        return ContextPolicy(
            input_budget_tokens=self.settings.agent_context_budget_tokens,
            compact_trigger_tokens=self.settings.agent_context_compact_trigger_tokens,
            recent_messages=self.settings.agent_context_recent_messages,
            summary_max_tokens=self.settings.agent_context_summary_max_tokens,
        )

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self.agent_service.initialize()
        self.run_service.initialize()
        self.memory_service.initialize()
        self._task = asyncio.create_task(
            self._loop(), name="agent-chat-worker"
        )
        self._wake.set()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def notify(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            run = self.run_service.claim(self.worker_id)
            if run is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._execute(run)
            except asyncio.CancelledError:
                with suppress(Exception):
                    self.run_service.requeue_interrupted(
                        run.id, reason="API worker stopped during generation"
                    )
                raise
            except OllamaTimeoutError as exc:
                if run.attempt_no < 2:
                    with suppress(ChatRunConflictError):
                        requeued = self.run_service.requeue_interrupted(
                            run.id,
                            reason=f"automatic watchdog retry: {exc}",
                        )
                        if requeued:
                            continue
                await self._fail_run(run, exc)
            except Exception as exc:
                await self._fail_run(run, exc)

    async def _execute(self, run: ChatRun) -> None:
        if not run.attempt_id:
            raise RuntimeError("claimed chat run is missing an attempt id")
        operation_id = f"chat-run:{run.id}"

        def ensure_session(current: AgentSession) -> AgentSession:
            if current.archived_at is not None:
                raise RuntimeError("archived sessions cannot run chat jobs")
            if current.active_operation not in {None, operation_id}:
                raise RuntimeError("the session is busy with another operation")
            messages = list(current.messages)
            if not any(
                message.role == "user"
                and (
                    message.metadata.get("chat_run_id") == run.id
                    or (
                        run.request_message_id is not None
                        and message.id == run.request_message_id
                    )
                )
                for message in messages
            ):
                message_metadata = {"chat_run_id": run.id}
                for key in (
                    "source",
                    "external_user_message_hash",
                    "parent_external_message_hash",
                ):
                    if run.metadata.get(key) is not None:
                        message_metadata[key] = run.metadata[key]
                current = self.agent_service.append_message(
                    current,
                    "user",
                    "chat",
                    run.input_text,
                    metadata=message_metadata,
                    message_id=run.request_message_id,
                )
            return current.model_copy(
                update={
                    "active_operation": operation_id,
                    "operation_started_at": datetime.now(timezone.utc),
                }
            )

        session = self.agent_service.mutate(run.session_id, ensure_session)
        existing_reply = next(
            (
                message
                for message in session.messages
                if message.role == "assistant"
                and message.metadata.get("chat_run_id") == run.id
            ),
            None,
        )
        if existing_reply is not None:

            def release_existing(current: AgentSession) -> AgentSession:
                if current.active_operation != operation_id:
                    return current
                return current.model_copy(
                    update={
                        "active_operation": None,
                        "operation_started_at": None,
                    }
                )

            self.agent_service.mutate(run.session_id, release_existing)
            self._store_conversation_memory(
                run=run,
                session=session,
                assistant_text=existing_reply.content,
            )
            self.run_service.complete(
                run.id,
                existing_reply.content,
                attempt_id=run.attempt_id,
                idempotency_key=f"complete-existing:{run.id}",
            )
            return

        user_index = next(
            index
            for index, message in enumerate(session.messages)
            if message.role == "user"
            and (
                message.metadata.get("chat_run_id") == run.id
                or (
                    run.request_message_id is not None
                    and message.id == run.request_message_id
                )
            )
        )
        prior_session = session.model_copy(
            update={"messages": session.messages[:user_index]}
        )
        policy = self.policy()
        compaction = plan_compaction(prior_session, policy)
        compacted_summary: str | None = None
        compaction_model: str | None = None
        context_session = prior_session
        if compaction is not None:
            self.run_service.append_event(
                run.id,
                "compaction_started",
                {"message_count": len(compaction.messages)},
                attempt_id=run.attempt_id,
                idempotency_key=f"compaction-start:{run.attempt_id}",
            )
            try:
                summary_reply = await self.ollama.chat(
                    message=compaction.prompt,
                    system=(
                        "Maintain durable memory using only supplied history. "
                        "Preserve exact constraints and source references."
                    ),
                    num_predict=policy.summary_max_tokens,
                )
                compacted_summary = trim_to_tokens(
                    summary_reply.get("message", {}).get("content", "").strip(),
                    policy.summary_max_tokens,
                )
                compaction_model = str(summary_reply.get("model") or "") or None
            except OllamaError:
                compacted_summary = None
            if compacted_summary:
                context_session = prior_session.model_copy(
                    update={
                        "rolling_summary": compacted_summary,
                        "compacted_message_count": compaction.through_count,
                        "compaction_count": prior_session.compaction_count + 1,
                        "last_compacted_at": datetime.now(timezone.utc),
                        "last_compaction_model": compaction_model,
                        "last_compaction_source_hash": compaction.source_hash,
                    }
                )

        evidence = await self._knowledge_context(session, run.input_text)
        memory_context = self._memory_context(session, run.input_text)
        envelope = build_context_envelope(
            context_session,
            current_message=run.input_text,
            knowledge_context=evidence,
            memory_context=memory_context,
            policy=policy,
        )
        self.run_service.append_event(
            run.id,
            "context_ready",
            envelope.telemetry.model_dump(mode="json"),
            attempt_id=run.attempt_id,
            idempotency_key=f"context:{run.attempt_id}",
        )

        models = await self.ollama.list_models()
        model = self.ollama.select_model(models)
        output: list[str] = []
        buffer: list[str] = []
        buffer_length = 0
        last_flush = monotonic()
        final_chunk: dict = {}
        event_index = 0

        async for chunk in self._controlled_stream(
            run,
            message=envelope.model_message,
            model=model,
            system=envelope.system_prompt,
        ):
            token = str(chunk.get("message", {}).get("content", ""))
            if token:
                output.append(token)
                buffer.append(token)
                buffer_length += len(token)
            final_chunk = chunk
            if buffer and (
                event_index == 0
                or buffer_length >= 24
                or monotonic() - last_flush >= 0.25
                or chunk.get("done")
            ):
                text = "".join(buffer)
                event_index += 1
                self.run_service.append_event(
                    run.id,
                    "token",
                    {"content": text, "model": model},
                    partial_text=text,
                    attempt_id=run.attempt_id,
                    idempotency_key=f"token:{run.attempt_id}:{event_index}",
                )
                buffer.clear()
                buffer_length = 0
                last_flush = monotonic()

        content = "".join(output).strip() or "The local model returned no content."
        telemetry = envelope.telemetry.model_copy(
            update={
                "model_prompt_tokens": final_chunk.get("prompt_eval_count"),
                "model_output_tokens": final_chunk.get("eval_count"),
            }
        )

        def finish_session(current: AgentSession) -> AgentSession:
            if current.active_operation != operation_id:
                existing = next(
                    (
                        message
                        for message in current.messages
                        if message.role == "assistant"
                        and message.metadata.get("chat_run_id") == run.id
                    ),
                    None,
                )
                if existing is not None:
                    return current
                raise AgentConflictError("chat run lost its session lease")
            update = {
                "active_operation": None,
                "operation_started_at": None,
                "context_telemetry": telemetry,
            }
            if compaction is not None and compacted_summary:
                update.update(
                    rolling_summary=compacted_summary,
                    compacted_message_count=compaction.through_count,
                    compaction_count=current.compaction_count + 1,
                    last_compacted_at=context_session.last_compacted_at,
                    last_compaction_model=compaction_model,
                    last_compaction_source_hash=compaction.source_hash,
                )
            updated = current.model_copy(update=update)
            message_metadata = {"chat_run_id": run.id, "model": model}
            for key in (
                "source",
                "external_message_hash",
                "external_user_message_hash",
                "parent_external_message_hash",
            ):
                if run.metadata.get(key) is not None:
                    message_metadata[key] = run.metadata[key]
            return self.agent_service.append_message(
                updated,
                "assistant",
                "chat",
                content,
                metadata=message_metadata,
            )

        self.agent_service.mutate(run.session_id, finish_session)
        memory_ids = self._store_conversation_memory(
            run=run,
            session=session,
            assistant_text=content,
        )
        if memory_ids:
            self.run_service.append_event(
                run.id,
                "memory_updated",
                {"memory_ids": memory_ids},
                attempt_id=run.attempt_id,
                idempotency_key=f"memory:{run.attempt_id}",
            )
        self.run_service.complete(
            run.id,
            content,
            attempt_id=run.attempt_id,
            idempotency_key=f"complete:{run.attempt_id}",
        )

    async def _controlled_stream(
        self,
        run: ChatRun,
        *,
        message: str,
        model: str,
        system: str | None,
    ) -> AsyncIterator[dict]:
        """Keep cancellation responsive and prevent one stalled model blocking the queue."""

        iterator = self.ollama.stream_chat(
            message=message,
            model=model,
            system=system,
        ).__aiter__()
        first_timeout = max(1.0, self.settings.ollama_first_token_timeout_seconds)
        idle_timeout = max(1.0, self.settings.ollama_stream_idle_timeout_seconds)
        first_deadline = monotonic() + first_timeout
        idle_deadline = first_deadline
        received_content = False
        try:
            while True:
                next_chunk = asyncio.create_task(anext(iterator))
                try:
                    while not next_chunk.done():
                        current = self.run_service.get(run.id)
                        if (
                            current is None
                            or current.status != ChatRunStatus.RUNNING
                            or current.attempt_id != run.attempt_id
                        ):
                            raise _RunCancelled(
                                "chat run was cancelled or superseded"
                            )
                        deadline = idle_deadline if received_content else first_deadline
                        remaining = deadline - monotonic()
                        if remaining <= 0:
                            phase = "stream idle" if received_content else "first token"
                            raise OllamaTimeoutError(
                                f"Ollama {phase} watchdog expired; the run was "
                                "stopped so later jobs can continue"
                            )
                        await asyncio.wait(
                            {next_chunk},
                            timeout=min(0.5, remaining),
                        )
                    try:
                        chunk = next_chunk.result()
                    except StopAsyncIteration:
                        return
                except BaseException:
                    if not next_chunk.done():
                        next_chunk.cancel()
                    with suppress(asyncio.CancelledError, StopAsyncIteration):
                        await next_chunk
                    raise

                token = str(chunk.get("message", {}).get("content", ""))
                if token:
                    received_content = True
                    idle_deadline = monotonic() + idle_timeout
                yield chunk
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                with suppress(Exception):
                    await close()

    async def _knowledge_context(
        self, session: AgentSession, query: str
    ) -> str:
        matches: list[dict] = []
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                if session.project_id:
                    response = await client.post(
                        self.settings.knowledge_service_url
                        + f"/projects/{session.project_id}/search",
                        json={"query": query, "limit": 6},
                    )
                    if response.is_success:
                        matches = self._relevant(response.json().get("matches", []))
                if not matches:
                    response = await client.post(
                        self.settings.knowledge_service_url + "/search",
                        json={"query": query, "limit": 6},
                    )
                    if response.is_success:
                        matches = self._relevant(response.json().get("matches", []))
        except (httpx.HTTPError, ValueError):
            matches = []
        return "\n\n".join(
            (
                f"[source={item.get('source') or 'unknown'}; "
                f"project={item.get('source_project_id') or 'unknown'}; "
                f"chunk={item.get('chunk_id') or item.get('chunk') or 'unknown'}; "
                f"sha256={item.get('source_sha256') or 'unknown'}]\n"
                f"{str(item.get('text') or '')[:1400]}"
            )
            for item in matches[:6]
        )

    def _memory_context(self, session: AgentSession, query: str) -> str:
        memories: list[MemoryRecord] = []
        # Stable user-level rules are intentionally available across projects.
        # Episodes, stage experiences, and decisions are never taken from global
        # scope because they may contain another chat branch or project's result.
        for kind in (MemoryKind.PREFERENCE, MemoryKind.CONSTRAINT):
            memories.extend(
                self.memory_service.list(
                    scope=MemoryScope.GLOBAL,
                    kind=kind,
                    min_confidence=0.7,
                    limit=10,
                )
            )
        memories.extend(
            item.memory
            for item in self.memory_service.search(
                query,
                scope=MemoryScope.GLOBAL,
                kinds=_GLOBAL_RETRIEVAL_KINDS,
                min_confidence=0.6,
                limit=10,
            )
        )
        if session.project_id:
            for kind in (MemoryKind.CONSTRAINT, MemoryKind.DECISION):
                memories.extend(
                    self.memory_service.list(
                        scope=MemoryScope.PROJECT,
                        project_id=session.project_id,
                        kind=kind,
                        min_confidence=0.7,
                        limit=10,
                    )
                )
            memories.extend(
                item.memory
                for item in self.memory_service.search(
                    query,
                    scope=MemoryScope.PROJECT,
                    project_id=session.project_id,
                    include_global=False,
                    min_confidence=0.6,
                    limit=10,
                )
            )
        unique: list[MemoryRecord] = []
        seen: set[str] = set()
        for memory in memories:
            if memory.id in seen:
                continue
            seen.add(memory.id)
            unique.append(memory)
            if len(unique) >= 16:
                break
        return "\n".join(
            f"- [{item.scope.value}/{item.kind.value}; {item.source}] "
            f"{item.content}"
            for item in unique
        )

    def _store_conversation_memory(
        self,
        *,
        run: ChatRun,
        session: AgentSession,
        assistant_text: str,
    ) -> list[str]:
        if bool(run.metadata.get("suppress_memory")):
            return []
        source = str(run.metadata.get("source") or "workbench")
        if source not in {"workbench", "open-webui"}:
            source = "workbench"
        memory_inputs = explicit_memory_candidates(
            run.input_text,
            source=source,
            source_ref=run.id,
            project_id=session.project_id,
        )
        if session.project_id:
            memory_inputs.append(
                episode_memory(
                    user_text=run.input_text,
                    assistant_text=assistant_text,
                    source=source,
                    source_ref=run.id,
                    project_id=session.project_id,
                    conversation_id=session.id,
                )
            )
        memory_ids: list[str] = []
        for candidate in memory_inputs:
            with suppress(Exception):
                memory_ids.append(self.memory_service.create(candidate).id)
        return memory_ids

    @staticmethod
    def _relevant(matches: list[dict]) -> list[dict]:
        unique: list[dict] = []
        seen: set[str] = set()
        for item in matches:
            if float(item.get("distance", float("inf"))) > 1.0:
                continue
            text = str(item.get("text") or "")
            if not text or text in seen:
                continue
            seen.add(text)
            unique.append(item)
        return unique

    async def _fail_run(self, run: ChatRun, exc: Exception) -> None:
        operation_id = f"chat-run:{run.id}"
        current = self.agent_service.get_session(run.session_id)
        persisted_reply = next(
            (
                message
                for message in (current.messages if current else [])
                if message.role == "assistant"
                and message.metadata.get("chat_run_id") == run.id
            ),
            None,
        )
        if persisted_reply is not None and run.attempt_id:
            with suppress(ChatRunConflictError):
                self.run_service.complete(
                    run.id,
                    persisted_reply.content,
                    attempt_id=run.attempt_id,
                    idempotency_key=f"complete-recovery:{run.attempt_id}",
                )
            if current is not None and current.active_operation == operation_id:
                with suppress(AgentConflictError, AgentDeletedError):
                    self.agent_service.mutate(
                        run.session_id,
                        lambda value: value.model_copy(
                            update={
                                "active_operation": None,
                                "operation_started_at": None,
                            }
                        )
                        if value.active_operation == operation_id
                        else value,
                    )
            return
        if isinstance(exc, _RunCancelled):
            message = "The background response was cancelled."
        else:
            message = f"The background response failed: {exc}"
            if run.attempt_id:
                with suppress(ChatRunConflictError):
                    self.run_service.fail(
                        run.id,
                        str(exc)[:2_000] or exc.__class__.__name__,
                        attempt_id=run.attempt_id,
                    )

        def release(current: AgentSession) -> AgentSession:
            if current.active_operation != operation_id:
                return current
            updated = current.model_copy(
                update={"active_operation": None, "operation_started_at": None}
            )
            return self.agent_service.append_message(
                updated,
                "assistant",
                "error" if not isinstance(exc, _RunCancelled) else "cancelled",
                message,
                AgentPhase.IMPLEMENTATION,
                {"chat_run_id": run.id},
            )

        with suppress(AgentConflictError, AgentDeletedError):
            self.agent_service.mutate(run.session_id, release)


_worker: AgentChatWorker | None = None


async def start_agent_chat_worker(
    *,
    settings: Settings | None = None,
    ollama: OllamaClient | None = None,
) -> AgentChatWorker:
    global _worker
    if _worker is None:
        _worker = AgentChatWorker(settings=settings, ollama=ollama)
    await _worker.start()
    return _worker


async def stop_agent_chat_worker() -> None:
    global _worker
    if _worker is not None:
        await _worker.stop()
        _worker = None


def notify_agent_chat_worker() -> None:
    if _worker is not None:
        _worker.notify()
