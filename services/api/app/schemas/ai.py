from typing import Any

from pydantic import BaseModel, Field, field_validator


class ReadyResponse(BaseModel):
    status: str
    service: str
    model_count: int
    default_model: str


class ModelInfo(BaseModel):
    name: str
    model: str | None = None
    modified_at: str | None = None
    size: int | None = None
    digest: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)


class ModelListResponse(BaseModel):
    models: list[ModelInfo]
    count: int
    default_model: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    system: str | None = Field(default=None, max_length=20_000)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value

    @field_validator("model", "system")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ChatResponse(BaseModel):
    model: str
    response: str
    done: bool
    done_reason: str | None = None
    total_duration: int | None = None
    load_duration: int | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration: int | None = None
    eval_count: int | None = None
    eval_duration: int | None = None
