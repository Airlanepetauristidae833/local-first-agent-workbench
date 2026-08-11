import asyncio

import httpx
import pytest

from app.config import get_settings
from app.main import app
from app.routers import ai as ai_router
from app.services.ollama_client import (
    OllamaClient,
    OllamaResponseError,
    OllamaUnavailableError,
    get_ollama_client,
)


def test_api_health_is_independent_from_ollama(client) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_localhost_model_endpoint_does_not_use_an_api_key(client) -> None:
    public_health = client.get("/api/v1/health")
    missing = client.get("/api/v1/models")
    wrong = client.get("/api/v1/models", headers={"X-API-Key": "wrong"})
    assert public_health.status_code == 200
    assert missing.status_code == 200
    assert wrong.status_code == 200


def test_ready_reports_default_model(client) -> None:
    response = client.get("/api/v1/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "ollama",
        "model_count": 1,
        "default_model": "test-model",
    }


def test_models_returns_installed_models(client) -> None:
    response = client.get("/api/v1/models")
    assert response.status_code == 200
    document = response.json()
    assert document["count"] == 1
    assert document["default_model"] == "test-model"
    assert document["models"][0]["name"] == "test-model"


def test_non_streaming_chat(client) -> None:
    response = client.post("/api/v1/chat", json={"message": "hello"})
    assert response.status_code == 200
    assert response.json() == {
        "model": "test-model",
        "response": "reply: hello",
        "done": True,
        "done_reason": "stop",
        "total_duration": 100,
        "load_duration": None,
        "prompt_eval_count": None,
        "prompt_eval_duration": None,
        "eval_count": 2,
        "eval_duration": 50,
    }


def test_blank_chat_message_is_rejected(client) -> None:
    response = client.post("/api/v1/chat", json={"message": "   "})
    assert response.status_code == 422


def test_unknown_model_returns_404(client) -> None:
    response = client.post(
        "/api/v1/chat",
        json={"message": "hello", "model": "missing-model"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "model_not_found"


def test_streaming_chat_is_sse(client) -> None:
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "hello"},
    ) as response:
        body = "".join(response.iter_text())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type":"token","content":"\u76d0"' in body
    assert '"type":"token","content":"\u57ce"' in body
    assert '"type":"done","model":"test-model"' in body


def test_ollama_payload_sets_explicit_context_and_output_limits(client) -> None:
    payload = OllamaClient(get_settings())._chat_payload(
        message="hello",
        model="test-model",
        system="system",
        stream=False,
    )
    assert payload["options"] == {"num_ctx": 16384, "num_predict": 4096}


def test_ready_is_503_while_health_stays_200(client) -> None:
    class UnavailableOllama:
        async def list_models(self):
            raise OllamaUnavailableError("Ollama is unreachable")

    app.dependency_overrides[get_ollama_client] = UnavailableOllama
    ready = client.get("/api/v1/ready")
    health = client.get("/api/v1/health")
    assert ready.status_code == 503
    assert ready.json()["detail"]["code"] == "ollama_not_ready"
    assert health.status_code == 200


def test_chat_does_not_expose_upstream_error_details(client, monkeypatch) -> None:
    secret = "private upstream body: user prompt and C:" + r"\secret\model.bin"
    log_calls: list[tuple[str, tuple[object, ...]]] = []

    class FailingOllama:
        async def chat(self, **_kwargs):
            raise OllamaResponseError(502, secret)

    def capture_log(message: str, *args: object, **_kwargs: object) -> None:
        log_calls.append((message, args))

    monkeypatch.setattr(ai_router.logger, "warning", capture_log)
    app.dependency_overrides[get_ollama_client] = FailingOllama

    response = client.post("/api/v1/chat", json={"message": "hello"})

    assert response.status_code == 502
    assert response.json()["detail"] == {
        "code": "ollama_error",
        "message": "Ollama request failed",
    }
    assert secret not in response.text
    assert len(log_calls) == 1
    rendered_log = log_calls[0][0] % log_calls[0][1]
    assert "event=ollama_request_failed" in rendered_log
    assert "error_type=OllamaResponseError" in rendered_log
    assert "upstream_status=502" in rendered_log
    assert secret not in rendered_log


def test_stream_does_not_expose_upstream_error_details(client, monkeypatch) -> None:
    secret = "private streamed body: user prompt and access token"
    log_calls: list[tuple[str, tuple[object, ...]]] = []

    class FailingStreamOllama:
        async def list_models(self):
            return [{"name": "test-model", "model": "test-model"}]

        def select_model(self, _models, _requested_model=None):
            return "test-model"

        async def stream_chat(self, **_kwargs):
            if False:
                yield {}
            raise OllamaResponseError(502, secret)

    def capture_log(message: str, *args: object, **_kwargs: object) -> None:
        log_calls.append((message, args))

    monkeypatch.setattr(ai_router.logger, "warning", capture_log)
    app.dependency_overrides[get_ollama_client] = FailingStreamOllama

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "hello"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"code":"ollama_error"' in body
    assert '"message":"Ollama request failed"' in body
    assert secret not in body
    assert len(log_calls) == 1
    rendered_log = log_calls[0][0] % log_calls[0][1]
    assert "operation=stream" in rendered_log
    assert "error_type=OllamaResponseError" in rendered_log
    assert "upstream_status=502" in rendered_log
    assert secret not in rendered_log


def test_connection_reset_is_normalized_to_ollama_error(monkeypatch) -> None:
    class BrokenClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, *_args, **_kwargs):
            raise httpx.ReadError(
                "connection reset",
                request=httpx.Request("GET", "http://ollama.test/api/tags"),
            )

    monkeypatch.setattr(
        "app.services.ollama_client.httpx.AsyncClient",
        lambda **_kwargs: BrokenClient(),
    )

    with pytest.raises(OllamaUnavailableError, match="ReadError"):
        asyncio.run(OllamaClient(get_settings()).list_models())
