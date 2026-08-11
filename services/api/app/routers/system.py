import asyncio

import httpx
from fastapi import APIRouter, Depends

from app.config import get_settings
from app.core.logging import get_logger
from app.schemas.service import HealthResponse, ServiceListResponse
from app.services.agent_service import AgentService
from app.services.ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaResponseError,
    get_ollama_client,
)
from app.services.orchestration_service import OrchestrationService
from app.services.service_registry import ServiceRegistry, get_service_registry
from app.services.workspace_service import WorkspaceService, get_workspace_service

router = APIRouter(tags=["system"])
logger = get_logger("routers.system")


@router.get("/health", response_model=HealthResponse, include_in_schema=False)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/api/v1/system/services", response_model=ServiceListResponse)
def services(
    registry: ServiceRegistry = Depends(get_service_registry),
) -> ServiceListResponse:
    items = registry.list()
    return ServiceListResponse(items=items, count=len(items))


async def _probe(url: str, params: dict | None = None) -> tuple[bool, dict]:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            headers = None
            if url.rstrip("/").endswith("/healthz"):
                headers = {
                    "X-Forwarded-For": "127.0.0.1",
                    "X-Real-IP": "127.0.0.1",
                }
            response = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError:
        return False, {}
    if not response.is_success:
        return False, {}
    try:
        document = response.json()
    except ValueError:
        # SearXNG /healthz intentionally returns plain text.  A successful
        # liveness response must not be marked unhealthy merely for lacking JSON.
        document = {}
    return True, document if isinstance(document, dict) else {}


@router.get("/api/v1/system/overview")
async def overview(
    ollama: OllamaClient = Depends(get_ollama_client),
    workspaces: WorkspaceService = Depends(get_workspace_service),
) -> dict:
    settings = get_settings()
    agent = AgentService(settings.agent_store_path)
    orchestration = OrchestrationService(settings.orchestrator_store_path)
    sessions = [
        item
        for item in agent.list_sessions(include_archived=True)
        if not item.internal
    ]
    plans = orchestration.list_plans()
    knowledge_result, search_result = await asyncio.gather(
        _probe(settings.knowledge_service_url + "/health"),
        # Use the local liveness endpoint.  A UI status refresh must not emit a
        # real internet search every ten seconds on every connected device.
        _probe(settings.search_service_url + "/healthz"),
    )
    knowledge_probe, knowledge_health = knowledge_result
    knowledge_index = knowledge_health.get("index", {})
    knowledge_healthy = (
        knowledge_probe
        and knowledge_health.get("status", "ok") == "ok"
        and int(knowledge_index.get("stale_projects", 0)) == 0
    )
    search_probe, _ = search_result
    try:
        models = await ollama.list_models()
        model = ollama.select_model(models)
        ollama_state = {"healthy": True, "model": model, "models": len(models)}
    except OllamaError as exc:
        upstream_status = (
            exc.status_code if isinstance(exc, OllamaResponseError) else "-"
        )
        logger.warning(
            "event=ollama_overview_failed error_type=%s upstream_status=%s",
            exc.__class__.__name__,
            upstream_status,
        )
        ollama_state = {
            "healthy": False,
            "model": None,
            "models": 0,
            "error": "Ollama status is unavailable",
        }
    return {
        "services": {
            "api": {"healthy": True},
            "ollama": ollama_state,
            "knowledge": {**knowledge_health, "healthy": knowledge_healthy},
            "search": {"healthy": search_probe},
        },
        "counts": {
            "sessions": len([item for item in sessions if item.archived_at is None]),
            "archived_sessions": len([item for item in sessions if item.archived_at is not None]),
            "running_sessions": len([item for item in sessions if item.status.value.endswith("running")]),
            "workspaces": len(workspaces.list_workspaces()),
            "pending_codex": len([plan for plan in plans if plan.status in {"handoff_pending", "codex_running"}]),
            "degraded_indexes": int(knowledge_index.get("stale_projects", 0)),
        },
        "entrypoints": {
            "open_webui": settings.public_open_webui_url,
            "agent": settings.public_agent_url,
        },
    }
