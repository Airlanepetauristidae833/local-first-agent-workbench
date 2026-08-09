from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.config import get_settings
from app.schemas.memory import (
    MemoryCreate,
    MemoryKind,
    MemoryList,
    MemoryRecord,
    MemoryScope,
    MemorySearchResult,
    MemoryUpdate,
)
from app.services.memory_service import (
    MemoryConflictError,
    MemoryDuplicateError,
    MemoryService,
)

router = APIRouter(prefix="/api/v1/memories", tags=["memory"])


def get_memory_service() -> MemoryService:
    service = MemoryService(get_settings().agent_store_path)
    service.initialize()
    return service


def _not_found(memory_id: str) -> HTTPException:
    return HTTPException(404, f"memory '{memory_id}' was not found")


def _conflict(exc: MemoryConflictError | MemoryDuplicateError) -> HTTPException:
    if isinstance(exc, MemoryDuplicateError):
        return HTTPException(
            409,
            {
                "message": "the update would duplicate an existing memory",
                "existing_id": exc.existing_id,
            },
        )
    return HTTPException(409, str(exc))


@router.post("", response_model=MemoryRecord, status_code=status.HTTP_201_CREATED)
def create_memory(
    request: MemoryCreate,
    service: MemoryService = Depends(get_memory_service),
) -> MemoryRecord:
    return service.create(request)


@router.get("", response_model=MemoryList)
def list_memories(
    scope: MemoryScope | None = None,
    project_id: str | None = Query(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,62}$",
    ),
    kind: MemoryKind | None = None,
    source: str | None = Query(default=None, min_length=1, max_length=500),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: MemoryService = Depends(get_memory_service),
) -> MemoryList:
    try:
        items = service.list(
            scope=scope,
            project_id=project_id,
            kind=kind,
            source=source,
            min_confidence=min_confidence,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return MemoryList(items=items, count=len(items))


@router.get("/search", response_model=list[MemorySearchResult])
def search_memories(
    query: str = Query(min_length=1, max_length=2_000),
    scope: MemoryScope | None = None,
    project_id: str | None = Query(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,62}$",
    ),
    include_global: bool = True,
    kinds: list[MemoryKind] | None = Query(default=None),
    min_confidence: float = Query(default=0.0, ge=0.0, le=1.0),
    limit: int = Query(default=20, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: MemoryService = Depends(get_memory_service),
) -> list[MemorySearchResult]:
    try:
        return service.search(
            query,
            scope=scope,
            project_id=project_id,
            include_global=include_global,
            kinds=kinds,
            min_confidence=min_confidence,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{memory_id}", response_model=MemoryRecord)
def get_memory(
    memory_id: str,
    service: MemoryService = Depends(get_memory_service),
) -> MemoryRecord:
    memory = service.get(memory_id)
    if memory is None:
        raise _not_found(memory_id)
    return memory


@router.patch("/{memory_id}", response_model=MemoryRecord)
def update_memory(
    memory_id: str,
    request: MemoryUpdate,
    expected_revision: int | None = Query(default=None, ge=1),
    service: MemoryService = Depends(get_memory_service),
) -> MemoryRecord:
    try:
        memory = service.update(
            memory_id,
            request,
            expected_revision=expected_revision,
        )
    except (MemoryConflictError, MemoryDuplicateError) as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if memory is None:
        raise _not_found(memory_id)
    return memory


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(
    memory_id: str,
    expected_revision: int | None = Query(default=None, ge=1),
    service: MemoryService = Depends(get_memory_service),
) -> Response:
    try:
        deleted = service.delete(memory_id, expected_revision=expected_revision)
    except MemoryConflictError as exc:
        raise _conflict(exc) from exc
    if not deleted:
        raise _not_found(memory_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
