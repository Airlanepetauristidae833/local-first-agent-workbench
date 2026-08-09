from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.schemas.ai import (
    ChatRequest,
    ChatResponse,
    ModelInfo,
    ModelListResponse,
    ReadyResponse,
)
from app.schemas.service import HealthResponse
from app.services.ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaProtocolError,
    OllamaResponseError,
    OllamaTimeoutError,
    OllamaUnavailableError,
    get_ollama_client,
)

router = APIRouter(prefix="/api/v1", tags=["ai"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse)
async def ready(
    client: OllamaClient = Depends(get_ollama_client),
) -> ReadyResponse:
    try:
        models = await client.list_models()
        selected_model = client.select_model(models)
    except OllamaError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "ollama_not_ready", "message": str(exc)},
        ) from exc
    return ReadyResponse(
        status="ready",
        service="ollama",
        model_count=len(models),
        default_model=selected_model,
    )


@router.get("/models", response_model=ModelListResponse)
async def models(
    client: OllamaClient = Depends(get_ollama_client),
) -> ModelListResponse:
    try:
        items = await client.list_models()
        default_model = client.select_model(items)
    except OllamaError as exc:
        raise _http_error(exc) from exc
    return ModelListResponse(
        models=[ModelInfo.model_validate(item) for item in items],
        count=len(items),
        default_model=default_model,
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    client: OllamaClient = Depends(get_ollama_client),
) -> ChatResponse:
    try:
        document = await client.chat(
            message=request.message,
            model=request.model,
            system=request.system,
        )
        return _chat_response(document)
    except OllamaError as exc:
        raise _http_error(exc) from exc


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    client: OllamaClient = Depends(get_ollama_client),
) -> StreamingResponse:
    try:
        models = await client.list_models()
        selected_model = client.select_model(models, request.model)
    except OllamaError as exc:
        raise _http_error(exc) from exc

    async def events() -> AsyncIterator[str]:
        done_sent = False
        try:
            async for chunk in client.stream_chat(
                message=request.message,
                model=selected_model,
                system=request.system,
            ):
                message = chunk.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str) and content:
                    yield _sse({"type": "token", "content": content})
                if chunk.get("done"):
                    done_sent = True
                    yield _sse(
                        {
                            "type": "done",
                            "model": str(chunk.get("model") or selected_model),
                            "done_reason": chunk.get("done_reason"),
                            "total_duration": chunk.get("total_duration"),
                            "eval_count": chunk.get("eval_count"),
                            "eval_duration": chunk.get("eval_duration"),
                        }
                    )
            if not done_sent:
                yield _sse(
                    {
                        "type": "error",
                        "code": "incomplete_stream",
                        "message": "Ollama stream ended without a done event",
                    }
                )
        except OllamaError as exc:
            yield _sse(
                {
                    "type": "error",
                    "code": _error_code(exc),
                    "message": str(exc),
                }
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _chat_response(document: dict[str, Any]) -> ChatResponse:
    message = document.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise OllamaProtocolError("Ollama chat response is missing message content")
    model = document.get("model")
    if not isinstance(model, str) or not model:
        raise OllamaProtocolError("Ollama chat response is missing the model name")
    return ChatResponse(
        model=model,
        response=message["content"],
        done=bool(document.get("done")),
        done_reason=_optional_string(document.get("done_reason")),
        total_duration=_optional_int(document.get("total_duration")),
        load_duration=_optional_int(document.get("load_duration")),
        prompt_eval_count=_optional_int(document.get("prompt_eval_count")),
        prompt_eval_duration=_optional_int(document.get("prompt_eval_duration")),
        eval_count=_optional_int(document.get("eval_count")),
        eval_duration=_optional_int(document.get("eval_duration")),
    )


def _http_error(exc: OllamaError) -> HTTPException:
    if isinstance(exc, OllamaUnavailableError):
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(exc, OllamaTimeoutError):
        code = status.HTTP_504_GATEWAY_TIMEOUT
    elif isinstance(exc, OllamaResponseError) and exc.status_code in {
        400,
        404,
        429,
        503,
    }:
        code = exc.status_code
    else:
        code = status.HTTP_502_BAD_GATEWAY
    return HTTPException(
        status_code=code,
        detail={"code": _error_code(exc), "message": str(exc)},
    )


def _error_code(exc: OllamaError) -> str:
    if isinstance(exc, OllamaUnavailableError):
        return "ollama_unavailable"
    if isinstance(exc, OllamaTimeoutError):
        return "ollama_timeout"
    if isinstance(exc, OllamaResponseError) and exc.status_code == 404:
        return "model_not_found"
    if isinstance(exc, OllamaProtocolError):
        return "ollama_invalid_response"
    return "ollama_error"


def _sse(document: dict[str, Any]) -> str:
    return f"data: {json.dumps(document, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
