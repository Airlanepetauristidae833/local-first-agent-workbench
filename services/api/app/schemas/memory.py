from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class MemoryScope(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"


class MemoryKind(str, Enum):
    PREFERENCE = "preference"
    CONSTRAINT = "constraint"
    FACT = "fact"
    DECISION = "decision"
    EXPERIENCE = "experience"
    EPISODE = "episode"


# A compatibility-friendly name for callers that describe the field as a type.
MemoryType = MemoryKind


class _MemoryContent(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    source: str = Field(default="user", min_length=1, max_length=500)
    source_ref: str | None = Field(default=None, max_length=2_000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content", "source")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("source_ref")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class MemoryCreate(_MemoryContent):
    scope: MemoryScope = MemoryScope.GLOBAL
    project_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,62}$",
    )
    kind: MemoryKind

    @model_validator(mode="after")
    def validate_scope(self) -> "MemoryCreate":
        if self.scope == MemoryScope.GLOBAL and self.project_id is not None:
            raise ValueError("global memories must not have a project_id")
        if self.scope == MemoryScope.PROJECT and self.project_id is None:
            raise ValueError("project memories require a project_id")
        return self


class MemoryUpdate(BaseModel):
    scope: MemoryScope | None = None
    project_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,62}$",
    )
    kind: MemoryKind | None = None
    content: str | None = Field(default=None, min_length=1, max_length=20_000)
    source: str | None = Field(default=None, min_length=1, max_length=500)
    source_ref: str | None = Field(default=None, max_length=2_000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    metadata: dict[str, Any] | None = None

    @field_validator("content", "source")
    @classmethod
    def strip_optional_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("source_ref")
    @classmethod
    def strip_source_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def require_change(self) -> "MemoryUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class MemoryRecord(_MemoryContent):
    id: str
    scope: MemoryScope
    project_id: str | None = None
    kind: MemoryKind
    revision: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_scope(self) -> "MemoryRecord":
        if self.scope == MemoryScope.GLOBAL and self.project_id is not None:
            raise ValueError("global memories must not have a project_id")
        if self.scope == MemoryScope.PROJECT and self.project_id is None:
            raise ValueError("project memories require a project_id")
        return self


class MemorySearchResult(BaseModel):
    memory: MemoryRecord
    score: float = Field(ge=0.0)
    matched_terms: list[str] = Field(default_factory=list)


class MemoryList(BaseModel):
    items: list[MemoryRecord]
    count: int = Field(ge=0)
