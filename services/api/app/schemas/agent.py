from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class AgentPhase(str, Enum):
    ANALYSIS = "analysis"
    KNOWLEDGE = "knowledge"
    IMPLEMENTATION = "implementation"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentStatus(str, Enum):
    ANALYZING = "analyzing"
    AWAITING_KNOWLEDGE_APPROVAL = "awaiting_knowledge_approval"
    WAITING_FOR_STAGE = "waiting_for_stage"
    LOCAL_RUNNING = "local_running"
    CODEX_RUNNING = "codex_running"
    COMPLETED = "completed"
    FAILED = "failed"


class KnowledgeState(str, Enum):
    CHECKING = "checking"
    AVAILABLE = "available"
    MISSING = "missing"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


class AgentMessage(BaseModel):
    id: str
    role: str
    kind: str
    content: str
    phase: AgentPhase
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextTelemetry(BaseModel):
    budget_tokens: int = Field(default=10_000, ge=1)
    estimated_input_tokens: int = Field(default=0, ge=0)
    current_message_tokens: int = Field(default=0, ge=0)
    current_message_truncated: bool = False
    summary_tokens: int = Field(default=0, ge=0)
    recent_message_tokens: int = Field(default=0, ge=0)
    knowledge_tokens: int = Field(default=0, ge=0)
    memory_tokens: int = Field(default=0, ge=0)
    model_prompt_tokens: int | None = Field(default=None, ge=0)
    model_output_tokens: int | None = Field(default=None, ge=0)
    compacted_message_count: int = Field(default=0, ge=0)
    compaction_count: int = Field(default=0, ge=0)
    last_compacted_at: datetime | None = None
    last_compaction_model: str | None = None
    last_compaction_source_hash: str | None = None


class KnowledgeSourceProposal(BaseModel):
    id: str
    name: str
    url: str
    query: str
    engine_group: str
    reason: str
    selected: bool = True


class AgentStage(BaseModel):
    id: str
    title: str = Field(max_length=200)
    description: str = Field(max_length=10_000)
    owner: str
    status: str = "pending"
    result: str = Field(default="", max_length=60_000)
    plan_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class AgentSessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=20_000)
    project_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,62}$")
    workspace_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,62}$")
    create_project: bool = True
    codex_context_consent: bool = False

    @field_validator("title", "goal")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value


class KnowledgeApproval(BaseModel):
    selected_source_ids: list[str] = Field(min_length=1, max_length=10)


class AgentAdvance(BaseModel):
    instruction: str = Field(default="", max_length=10_000)


class AgentChatMessage(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content must not be blank")
        return value


class AgentSessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    archived: bool | None = None
    codex_context_consent: bool | None = None

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def require_change(self) -> "AgentSessionUpdate":
        if (
            self.title is None
            and self.archived is None
            and self.codex_context_consent is None
        ):
            raise ValueError("at least one field must be provided")
        return self


class AgentSessionDelete(BaseModel):
    confirm_title: str = Field(min_length=1, max_length=120)


class AgentSession(BaseModel):
    id: str
    title: str
    goal: str
    internal: bool = False
    project_id: str | None = None
    workspace_id: str | None = None
    phase: AgentPhase
    status: AgentStatus
    execution_mode: str
    codex_context_consent: bool = False
    local_percent: int = Field(ge=0, le=100)
    codex_percent: int = Field(ge=0, le=100)
    routing_reason: str
    knowledge_state: KnowledgeState
    knowledge_matches: list[dict[str, Any]] = Field(default_factory=list)
    source_proposals: list[KnowledgeSourceProposal] = Field(default_factory=list)
    selected_source_ids: list[str] = Field(default_factory=list)
    research_note: str | None = None
    stages: list[AgentStage] = Field(default_factory=list)
    current_stage_index: int = 0
    messages: list[AgentMessage] = Field(default_factory=list)
    rolling_summary: str = Field(default="", max_length=20_000)
    compacted_message_count: int = Field(default=0, ge=0)
    compaction_count: int = Field(default=0, ge=0)
    last_compacted_at: datetime | None = None
    last_compaction_model: str | None = None
    last_compaction_source_hash: str | None = None
    context_telemetry: ContextTelemetry = Field(default_factory=ContextTelemetry)
    revision: int = Field(default=0, ge=0)
    active_operation: str | None = None
    operation_started_at: datetime | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AgentSessionList(BaseModel):
    items: list[AgentSession]
    count: int


class AgentSessionSummary(BaseModel):
    id: str
    title: str
    goal: str
    project_id: str | None = None
    workspace_id: str | None = None
    status: AgentStatus
    local_percent: int = Field(ge=0, le=100)
    current_stage_index: int
    stage_count: int
    completed_stages: int
    archived_at: datetime | None = None
    updated_at: datetime


class AgentSessionSummaryList(BaseModel):
    items: list[AgentSessionSummary]
    count: int
