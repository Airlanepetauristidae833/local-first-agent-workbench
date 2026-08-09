from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.schemas.workspace import (
    WorkspaceInfo,
    WorkspaceInspection,
    WorkspaceListResponse,
    WorkspaceSearch,
)
from app.services.workspace_service import (
    WorkspaceCapabilityError,
    WorkspaceNotFoundError,
    WorkspaceSearchQueryError,
    WorkspaceService,
    get_workspace_service,
)

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])


@router.get("", response_model=WorkspaceListResponse)
def list_workspaces(
    service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceListResponse:
    items = service.list_workspaces()
    return WorkspaceListResponse(items=items, count=len(items))


@router.get("/{workspace_id}", response_model=WorkspaceInfo)
def get_workspace(
    workspace_id: str,
    service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceInfo:
    try:
        return service.get_workspace(workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workspace not found",
        ) from exc


@router.get("/{workspace_id}/inspect", response_model=WorkspaceInspection)
def inspect_workspace(
    workspace_id: str,
    max_files: int = Query(default=5000, ge=1, le=100000),
    service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceInspection:
    try:
        return service.inspect(workspace_id, max_files=max_files)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workspace not found",
        ) from exc
    except WorkspaceCapabilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "workspace_capability_denied",
                "message": str(exc),
            },
        ) from exc


@router.get("/{workspace_id}/search", response_model=WorkspaceSearch)
def search_workspace(
    workspace_id: str,
    q: str = Query(min_length=1, max_length=200),
    case_sensitive: bool = Query(default=False),
    max_files: int = Query(default=1000, ge=1, le=5000),
    max_directories: int = Query(default=1000, ge=1, le=5000),
    max_results: int = Query(default=50, ge=1, le=200),
    max_file_bytes: int = Query(default=262144, ge=1, le=1048576),
    max_total_bytes: int = Query(default=5242880, ge=1, le=20971520),
    service: WorkspaceService = Depends(get_workspace_service),
) -> WorkspaceSearch:
    try:
        return service.search(
            workspace_id,
            q,
            case_sensitive=case_sensitive,
            max_files=max_files,
            max_directories=max_directories,
            max_results=max_results,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
    except WorkspaceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workspace not found",
        ) from exc
    except WorkspaceCapabilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "workspace_capability_denied",
                "message": str(exc),
            },
        ) from exc
    except WorkspaceSearchQueryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_search_query",
                "message": str(exc),
            },
        ) from exc
