from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import ValidationError

from app.schemas.ai import ChatRequest
from app.schemas.task import TaskCreate, TaskListResponse, TaskRecord
from app.schemas.workspace import WorkspaceInspectRequest, WorkspaceSearchRequest
from app.services.ollama_client import OllamaClient, get_ollama_client
from app.services.task_executor import (
    execute_ai_chat_task,
    execute_workspace_inspect_task,
    execute_workspace_search_task,
)
from app.services.task_service import TaskService, get_task_service
from app.services.workspace_service import WorkspaceService, get_workspace_service

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.post("", response_model=TaskRecord, status_code=status.HTTP_201_CREATED)
async def create_task(
    request: TaskCreate,
    background_tasks: BackgroundTasks,
    service: TaskService = Depends(get_task_service),
    ollama: OllamaClient = Depends(get_ollama_client),
    workspaces: WorkspaceService = Depends(get_workspace_service),
) -> TaskRecord:
    if request.name not in {"ai.chat", "workspace.inspect", "workspace.search"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unsupported_task",
                "message": (
                    "supported task names: ai.chat, workspace.inspect, "
                    "workspace.search"
                ),
            },
        )
    try:
        if request.name == "ai.chat":
            ChatRequest.model_validate(request.payload)
        elif request.name == "workspace.inspect":
            WorkspaceInspectRequest.model_validate(request.payload)
        else:
            WorkspaceSearchRequest.model_validate(request.payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_task_payload",
                "message": str(exc),
            },
        ) from exc
    task = service.create(request)
    if request.name == "ai.chat":
        background_tasks.add_task(
            execute_ai_chat_task,
            task,
            service,
            ollama,
        )
    elif request.name == "workspace.inspect":
        background_tasks.add_task(
            execute_workspace_inspect_task,
            task,
            service,
            workspaces,
        )
    else:
        background_tasks.add_task(
            execute_workspace_search_task,
            task,
            service,
            workspaces,
        )
    return task


@router.get("", response_model=TaskListResponse)
def list_tasks(
    service: TaskService = Depends(get_task_service),
) -> TaskListResponse:
    items = service.list_tasks()
    return TaskListResponse(items=items, count=len(items))


@router.get("/{task_id}", response_model=TaskRecord)
def get_task(
    task_id: str,
    service: TaskService = Depends(get_task_service),
) -> TaskRecord:
    task = service.get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return task
