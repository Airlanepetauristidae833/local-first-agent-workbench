from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ChatRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChatRun(BaseModel):
    id: str
    session_id: str
    request_message_id: str | None = None
    input_text: str
    status: ChatRunStatus
    partial_text: str = ""
    final_text: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    worker_id: str | None = None
    attempt_id: str | None = None
    attempt_no: int = Field(default=0, ge=0)
    last_event_seq: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None


class ChatRunEvent(BaseModel):
    run_id: str
    seq: int = Field(ge=1)
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    created_at: datetime


class ChatRunCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    suppress_memory: bool = False

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content must not be blank")
        return value


class ChatRunCancel(BaseModel):
    reason: str = Field(default="cancelled by user", min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value
