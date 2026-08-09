from __future__ import annotations

import json
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

import httpx

from app.config import Settings, get_settings


class OllamaError(RuntimeError):
    """Base error for Ollama connectivity and response failures."""


class OllamaUnavailableError(OllamaError):
    """Ollama could not be reached."""


class OllamaTimeoutError(OllamaError):
    """Ollama did not respond before the configured timeout."""


class OllamaProtocolError(OllamaError):
    """Ollama returned a response that does not match its API contract."""


class OllamaResponseError(OllamaError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.ollama_base_url
        self._configured_model = settings.ollama_model
        self._keep_alive = settings.ollama_keep_alive
        self._think = settings.ollama_think
        self._context_length = settings.ollama_context_length
        self._num_predict = settings.ollama_num_predict
        self._timeout = httpx.Timeout(
            timeout=settings.ollama_request_timeout_seconds,
            connect=settings.ollama_connect_timeout_seconds,
        )

    async def list_models(self) -> list[dict[str, Any]]:
        document = await self._request("GET", "/api/tags")
        models = document.get("models")
        if not isinstance(models, list):
            raise OllamaProtocolError("Ollama response is missing the models list")
        return [model for model in models if isinstance(model, dict)]

    def select_model(
        self,
        models: list[dict[str, Any]],
        requested_model: str | None = None,
    ) -> str:
        selected = (requested_model or self._configured_model or "").strip()
        installed = {
            value
            for model in models
            for value in (model.get("name"), model.get("model"))
            if isinstance(value, str)
        }
        if selected:
            if selected not in installed:
                raise OllamaResponseError(
                    404,
                    f"model '{selected}' is not installed",
                )
            return selected
        if not models:
            raise OllamaResponseError(503, "no Ollama models are installed")
        fallback = models[0].get("model") or models[0].get("name")
        if not isinstance(fallback, str) or not fallback:
            raise OllamaProtocolError("Ollama returned a model without a name")
        return fallback

    async def chat(
        self,
        message: str,
        model: str | None = None,
        system: str | None = None,
        num_predict: int | None = None,
    ) -> dict[str, Any]:
        models = await self.list_models()
        selected_model = self.select_model(models, model)
        return await self._request(
            "POST",
            "/api/chat",
            json_body=self._chat_payload(
                message=message,
                model=selected_model,
                system=system,
                stream=False,
                num_predict=num_predict,
            ),
        )

    async def stream_chat(
        self,
        message: str,
        model: str,
        system: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        payload = self._chat_payload(
            message=message,
            model=model,
            system=system,
            stream=True,
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            ) as client:
                async with client.stream("POST", "/api/chat", json=payload) as response:
                    if response.is_error:
                        body = await response.aread()
                        self._raise_response_error(response.status_code, body)
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise OllamaProtocolError(
                                "Ollama returned invalid streaming JSON"
                            ) from exc
                        if not isinstance(chunk, dict):
                            raise OllamaProtocolError(
                                "Ollama returned a non-object stream chunk"
                            )
                        if "error" in chunk:
                            raise OllamaResponseError(
                                502,
                                str(chunk["error"]),
                            )
                        yield chunk
        except httpx.ConnectError as exc:
            raise OllamaUnavailableError("Ollama is unreachable") from exc
        except httpx.TimeoutException as exc:
            raise OllamaTimeoutError("Ollama request timed out") from exc
        except httpx.RequestError as exc:
            raise OllamaUnavailableError(
                f"Ollama connection failed ({exc.__class__.__name__})"
            ) from exc

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            ) as client:
                response = await client.request(method, path, json=json_body)
        except httpx.ConnectError as exc:
            raise OllamaUnavailableError("Ollama is unreachable") from exc
        except httpx.TimeoutException as exc:
            raise OllamaTimeoutError("Ollama request timed out") from exc
        except httpx.RequestError as exc:
            raise OllamaUnavailableError(
                f"Ollama connection failed ({exc.__class__.__name__})"
            ) from exc

        if response.is_error:
            self._raise_response_error(response.status_code, response.content)
        try:
            document = response.json()
        except ValueError as exc:
            raise OllamaProtocolError("Ollama returned invalid JSON") from exc
        if not isinstance(document, dict):
            raise OllamaProtocolError("Ollama returned a non-object response")
        return document

    @staticmethod
    def _raise_response_error(status_code: int, body: bytes) -> None:
        try:
            document = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            document = {}
        message = document.get("error") if isinstance(document, dict) else None
        raise OllamaResponseError(
            status_code,
            str(message or f"Ollama returned HTTP {status_code}"),
        )

    def _chat_payload(
        self,
        message: str,
        model: str,
        system: str | None,
        stream: bool,
        num_predict: int | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": message})
        return {
            "model": model,
            "messages": messages,
            "stream": stream,
            "keep_alive": self._keep_alive,
            "think": self._think,
            "options": {
                "num_ctx": self._context_length,
                "num_predict": num_predict or self._num_predict,
            },
        }


@lru_cache
def get_ollama_client() -> OllamaClient:
    return OllamaClient(get_settings())
