from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.schemas.ai import ChatRequest
from app.schemas.task import TaskRecord
from app.schemas.workspace import WorkspaceInspectRequest, WorkspaceSearchRequest
from app.services.ollama_client import OllamaClient
from app.services.task_service import TaskService
from app.services.workspace_service import WorkspaceService

logger = get_logger("task_executor")


async def execute_ai_chat_task(
    task: TaskRecord,
    service: TaskService,
    client: OllamaClient,
) -> None:
    service.start(task.id)
    try:
        request = ChatRequest.model_validate(task.payload)
        document = await client.chat(
            message=request.message,
            model=request.model,
            system=request.system,
        )
        result = _chat_result(document)
        service.complete(task.id, result)
    except Exception as exc:
        logger.exception("Task failed id=%s name=%s", task.id, task.name)
        service.fail(task.id, _safe_error(exc))


async def execute_workspace_inspect_task(
    task: TaskRecord,
    service: TaskService,
    workspaces: WorkspaceService,
) -> None:
    service.start(task.id)
    try:
        request = WorkspaceInspectRequest.model_validate(task.payload)
        inspection = await asyncio.to_thread(
            workspaces.inspect,
            request.workspace_id,
            request.max_files,
        )
        service.complete(
            task.id,
            inspection.model_dump(mode="json"),
        )
    except Exception as exc:
        logger.exception("Task failed id=%s name=%s", task.id, task.name)
        service.fail(task.id, _safe_error(exc))


async def execute_workspace_search_task(
    task: TaskRecord,
    service: TaskService,
    workspaces: WorkspaceService,
) -> None:
    service.start(task.id)
    try:
        request = WorkspaceSearchRequest.model_validate(task.payload)
        search = await asyncio.to_thread(
            workspaces.search,
            request.workspace_id,
            request.query,
            case_sensitive=request.case_sensitive,
            max_files=request.max_files,
            max_directories=request.max_directories,
            max_results=request.max_results,
            max_file_bytes=request.max_file_bytes,
            max_total_bytes=request.max_total_bytes,
        )
        service.complete(
            task.id,
            search.model_dump(mode="json"),
        )
    except Exception as exc:
        logger.exception("Task failed id=%s name=%s", task.id, task.name)
        service.fail(task.id, _safe_error(exc))


def _chat_result(document: dict[str, Any]) -> dict[str, Any]:
    message = document.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise RuntimeError("Ollama chat response is missing message content")
    model = document.get("model")
    if not isinstance(model, str) or not model:
        raise RuntimeError("Ollama chat response is missing the model name")
    return {
        "model": model,
        "response": message["content"],
        "done": bool(document.get("done")),
        "done_reason": document.get("done_reason"),
        "total_duration": document.get("total_duration"),
        "eval_count": document.get("eval_count"),
        "eval_duration": document.get("eval_duration"),
    }


def _safe_error(exc: Exception) -> str:
    message = str(exc).strip()
    return message[:1000] or exc.__class__.__name__
