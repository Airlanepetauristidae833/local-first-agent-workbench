from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ConnectorStatus(str, Enum):
    AVAILABLE = "available"
    NOT_CONFIGURED = "not_configured"


class RouteRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    project_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,62}$")
    workspace_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,62}$")
    allow_online: bool = False
    allow_codex: bool = False

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("prompt must not be blank")
        return value


class CodexExecutionStart(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)


class CodexExecutionHeartbeat(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    attempt_id: str = Field(min_length=1, max_length=120)


class CodexExecutionComplete(BaseModel):
    worker_id: str = Field(min_length=1, max_length=120)
    attempt_id: str = Field(min_length=1, max_length=120)
    success: bool
    summary: str = Field(default="", max_length=20_000)
    output: str = Field(default="", max_length=60_000)
    workspace_path: str = Field(default="", max_length=2_000)
    changed_files: list[str] = Field(default_factory=list, max_length=500)
    validation: list[str] = Field(default_factory=list, max_length=200)
    error: str | None = Field(default=None, max_length=20_000)


class ExecutionPlan(BaseModel):
    id: str
    prompt: str
    intent: str
    local_workspace_id: str | None = None
    project_id: str | None = None
    connectors: list[str]
    authority: str
    approval_required: bool
    summary: str
    status: str
    created_at: datetime
    updated_at: datetime
    result: dict[str, Any] | None = None


class PlanList(BaseModel):
    items: list[ExecutionPlan]
    count: int
