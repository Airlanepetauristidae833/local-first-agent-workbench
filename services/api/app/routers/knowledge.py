from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import get_settings
from app.schemas.agent import AgentPhase, AgentStatus, KnowledgeState
from app.services.agent_service import (
    AgentConflictError,
    AgentDeletedError,
    AgentService,
)
from app.services.memory_service import MemoryService

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])
INDEX_TIMEOUT_SECONDS = 600.0


class KnowledgeProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @model_validator(mode="after")
    def require_change(self) -> "KnowledgeProjectUpdate":
        if self.name is None and self.archived is None:
            raise ValueError("at least one field must be provided")
        return self


class KnowledgeProjectDelete(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=120)
    trash_managed_files: bool = False
    detach_sessions: bool = False


def _agent_service() -> AgentService:
    service = AgentService(get_settings().agent_store_path)
    service.initialize()
    return service


def _memory_service() -> MemoryService:
    service = MemoryService(get_settings().agent_store_path)
    service.initialize()
    return service


async def _request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 60.0,
    params: dict[str, Any] | None = None,
) -> Any:
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.request(
                method,
                get_settings().knowledge_service_url + path,
                json=payload,
                params=params,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, {"code": "knowledge_service_unavailable", "message": str(exc)}) from exc
    if response.is_error:
        raise HTTPException(response.status_code, response.text)
    return response.json()


@router.get("/projects")
async def projects(include_archived: bool = Query(default=False)) -> Any:
    return await _request(
        "GET", "/projects", params={"include_archived": str(include_archived).lower()}
    )


@router.get("/projects/{project_id}")
async def project(project_id: str) -> Any:
    return await _request("GET", f"/projects/{project_id}")


@router.post("/projects")
async def create_project(payload: dict[str, Any]) -> Any:
    return await _request(
        "POST", "/projects", payload, timeout_seconds=INDEX_TIMEOUT_SECONDS
    )


@router.patch("/projects/{project_id}")
async def update_project(project_id: str, payload: KnowledgeProjectUpdate) -> Any:
    return await _request(
        "PATCH", f"/projects/{project_id}", payload.model_dump(exclude_none=True)
    )


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, payload: KnowledgeProjectDelete) -> Any:
    service = _agent_service()
    references = [
        session
        for session in service.list_sessions(include_archived=True)
        if session.project_id == project_id
    ]
    running = [
        session
        for session in references
        if session.active_operation
        or session.knowledge_state == KnowledgeState.BUILDING
        or session.status in {AgentStatus.LOCAL_RUNNING, AgentStatus.CODEX_RUNNING}
    ]
    if running:
        raise HTTPException(
            409,
            {
                "code": "knowledge_project_has_running_sessions",
                "message": (
                    "A local-model or Codex stage is still running. Wait for "
                    "it to finish before deleting the knowledge base."
                ),
                "count": len(running),
            },
        )
    if references and not payload.detach_sessions:
        raise HTTPException(
            409,
            {
                "code": "knowledge_project_in_use",
                "message": (
                    "The knowledge base is still referenced by project "
                    "sessions. Archive or delete those sessions first, or "
                    "detach them while preserving their history."
                ),
                "sessions": [
                    {"id": session.id, "title": session.title}
                    for session in references[:20]
                ],
                "count": len(references),
            },
        )
    operation_id = f"knowledge_delete:{project_id}"
    locked_ids: list[str] = []

    def lock_reference(current):
        if current.project_id != project_id:
            raise HTTPException(
                409,
                "The session's knowledge-base link changed. Refresh and retry.",
            )
        if (
            current.active_operation
            or current.knowledge_state == KnowledgeState.BUILDING
            or current.status in {AgentStatus.LOCAL_RUNNING, AgentStatus.CODEX_RUNNING}
        ):
            raise HTTPException(
                409,
                "The project session is running, so its knowledge base cannot be deleted.",
            )
        return current.model_copy(
            update={
                "active_operation": operation_id,
                "operation_started_at": datetime.now(timezone.utc),
            }
        )

    def release_reference(current):
        if current.active_operation != operation_id:
            return current
        return current.model_copy(
            update={"active_operation": None, "operation_started_at": None}
        )

    try:
        for session in references:
            service.mutate(session.id, lock_reference)
            locked_ids.append(session.id)
        result = await _request(
            "DELETE",
            f"/projects/{project_id}",
            {
                "confirm_name": payload.confirm_name,
                "trash_managed_files": payload.trash_managed_files,
            },
            timeout_seconds=INDEX_TIMEOUT_SECONDS,
        )
        # The knowledge service is the authoritative project registry, so local
        # memories are removed only after its irreversible delete succeeds.
        # Memory rows and their term index are then cascaded atomically in the
        # shared local SQLite store; failures are surfaced instead of returning
        # a misleading successful response.
        deleted_memories = _memory_service().delete_project_memories(project_id)
    except (AgentConflictError, AgentDeletedError) as exc:
        for session_id in locked_ids:
            try:
                service.mutate(session_id, release_reference)
            except (AgentConflictError, AgentDeletedError):
                pass
        raise HTTPException(
            409,
            "The project session was updated on another device. Refresh and retry.",
        ) from exc
    except Exception:
        for session_id in locked_ids:
            try:
                service.mutate(session_id, release_reference)
            except (AgentConflictError, AgentDeletedError):
                pass
        raise

    for session_id in locked_ids:
        def detach_reference(current):
            if current.active_operation != operation_id:
                raise HTTPException(
                    409,
                    "The project session's detach state changed.",
                )
            detached = current.model_copy(
                update={
                    "project_id": None,
                    "knowledge_state": KnowledgeState.MISSING,
                    "knowledge_matches": [],
                    "selected_source_ids": [],
                    "source_proposals": [],
                    "research_note": None,
                    "archived_at": datetime.now(timezone.utc),
                    "active_operation": None,
                    "operation_started_at": None,
                }
            )
            return service.append_message(
                detached,
                "assistant",
                "management",
                (
                    f'Knowledge base "{payload.confirm_name}" was deleted. '
                    "The project session was archived, its history was "
                    "preserved, and the knowledge-base link was removed."
                ),
                AgentPhase.KNOWLEDGE,
            )

        try:
            service.mutate(session_id, detach_reference)
        except AgentDeletedError:
            continue
    return {
        **result,
        "detached_sessions": len(references),
        "deleted_memories": deleted_memories,
    }


@router.post("/projects/{project_id}/search")
async def search(project_id: str, payload: dict[str, Any]) -> Any:
    return await _request("POST", f"/projects/{project_id}/search", payload)


@router.post("/search")
async def search_all(payload: dict[str, Any]) -> Any:
    return await _request("POST", "/search", payload)


@router.post("/projects/{project_id}/index")
async def index(project_id: str) -> Any:
    return await _request(
        "POST",
        f"/projects/{project_id}/index",
        timeout_seconds=INDEX_TIMEOUT_SECONDS,
    )


@router.post("/projects/{project_id}/handoff")
async def handoff(project_id: str, payload: dict[str, Any]) -> Any:
    return await _request(
        "POST",
        f"/projects/{project_id}/handoff",
        payload,
        timeout_seconds=INDEX_TIMEOUT_SECONDS,
    )
