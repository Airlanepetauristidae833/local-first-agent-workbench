from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.config import get_settings
from app.core.logging import configure_logging
from app.routers import (
    agent,
    ai,
    chat_runs,
    knowledge,
    memory,
    openai_compat,
    orchestration,
    system,
    tasks,
    workspaces,
)
from app.schemas.chat_run import ChatRunStatus
from app.schemas.service import ServiceStatus
from app.services.agent_chat_runner import (
    start_agent_chat_worker,
    stop_agent_chat_worker,
)
from app.services.agent_service import AgentService
from app.services.chat_run_service import ChatRunService
from app.services.ollama_client import get_ollama_client
from app.services.orchestration_service import OrchestrationService
from app.services.service_registry import get_service_registry
from app.services.task_service import get_task_service


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_directories()
    logger = configure_logging(settings.log_dir, settings.log_level)
    registry = get_service_registry()
    task_service = get_task_service()
    interrupted = task_service.fail_interrupted()
    agent_service = AgentService(settings.agent_store_path)
    chat_run_service = ChatRunService(settings.agent_store_path)
    requeued_chat_runs = chat_run_service.requeue_interrupted(
        reason="API process restarted"
    )
    orchestration_service = OrchestrationService(settings.orchestrator_store_path)
    valid_codex_plans = {
        plan.id
        for plan in orchestration_service.list_plans()
        if plan.status in {"handoff_pending", "codex_running", "completed", "failed"}
    }
    valid_chat_run_ids = {
        run.id
        for run in chat_run_service.list_runs(
            statuses={ChatRunStatus.QUEUED, ChatRunStatus.RUNNING}
        )
    }
    recovered_agent_sessions = agent_service.recover_interrupted(
        valid_codex_plans,
        valid_chat_run_ids,
    )
    ollama_provider = application.dependency_overrides.get(
        get_ollama_client, get_ollama_client
    )
    agent_chat_worker = await start_agent_chat_worker(
        settings=settings,
        ollama=ollama_provider(),
    )
    application.state.agent_chat_worker = agent_chat_worker
    registry.register("api", ServiceStatus.HEALTHY, settings.app_version)
    logger.info("API started version=%s", settings.app_version)
    if interrupted:
        logger.warning("Marked %d interrupted tasks as failed", len(interrupted))
    if recovered_agent_sessions:
        logger.warning(
            "Recovered %d interrupted Agent sessions", len(recovered_agent_sessions)
        )
    if requeued_chat_runs:
        logger.warning(
            "Requeued %d interrupted durable chat runs", len(requeued_chat_runs)
        )
    try:
        yield
    finally:
        await stop_agent_chat_worker()
        registry.register("api", ServiceStatus.STOPPED, settings.app_version)
        logger.info("API stopped")


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


app.include_router(system.router)
app.include_router(ai.router)
app.include_router(tasks.router)
app.include_router(workspaces.router)
app.include_router(orchestration.router)
app.include_router(agent.router)
app.include_router(chat_runs.router)
app.include_router(knowledge.router)
app.include_router(memory.router)
app.include_router(openai_compat.router)


@app.get("/console", include_in_schema=False)
def console() -> FileResponse:
    return FileResponse(
        "app/static/console.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )
