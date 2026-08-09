from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkspaceCapability(str, Enum):
    INSPECT = "inspect"
    SEARCH = "search"


class WorkspacePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_access: Literal["read-only"] = "read-only"
    command_execution: Literal["disabled"] = "disabled"
    command_allowlist: list[str] = Field(default_factory=list, max_length=50)
    human_approval_required_for: list[Literal["write", "command"]] = Field(
        default_factory=lambda: ["write", "command"]
    )

    @model_validator(mode="after")
    def reject_inactive_command_allowlist(self) -> "WorkspacePolicy":
        if self.command_allowlist:
            raise ValueError(
                "command_allowlist must remain empty while command_execution is disabled"
            )
        return self


class WorkspaceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    capabilities: list[WorkspaceCapability] = Field(
        default_factory=lambda: [WorkspaceCapability.INSPECT]
    )
    policy: WorkspacePolicy = Field(default_factory=WorkspacePolicy)


class WorkspaceInfo(BaseModel):
    id: str
    name: str
    description: str
    directory: str
    capabilities: list[WorkspaceCapability]
    policy: WorkspacePolicy


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceInfo]
    count: int


class WorkspaceExtensionStat(BaseModel):
    extension: str
    files: int
    bytes: int


class WorkspaceInspection(BaseModel):
    workspace: WorkspaceInfo
    file_count: int
    directory_count: int
    total_bytes: int
    extensions: list[WorkspaceExtensionStat]
    top_level_entries: list[str]
    ignored_directories: list[str]
    truncated: bool
    max_files: int
    inspected_at: datetime


class WorkspaceInspectRequest(BaseModel):
    workspace_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    max_files: int = Field(default=5000, ge=1, le=100000)


class WorkspaceSearchMatch(BaseModel):
    path: str
    line_number: int
    snippet: str


class WorkspaceSearchLimits(BaseModel):
    max_files: int
    max_directories: int
    max_results: int
    max_file_bytes: int
    max_total_bytes: int
    max_snippet_chars: int


class WorkspaceSearch(BaseModel):
    workspace: WorkspaceInfo
    query: str
    case_sensitive: bool
    directories_scanned: int
    files_scanned: int
    bytes_scanned: int
    skipped_files: int
    skipped_by_type: int
    skipped_by_size: int
    skipped_by_encoding: int
    skipped_unreadable: int
    matches: list[WorkspaceSearchMatch]
    searched_extensions: list[str]
    ignored_directories: list[str]
    limits: WorkspaceSearchLimits
    truncated: bool
    searched_at: datetime


class WorkspaceSearchRequest(BaseModel):
    workspace_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    query: str = Field(min_length=1, max_length=200)
    case_sensitive: bool = False
    max_files: int = Field(default=1000, ge=1, le=5000)
    max_directories: int = Field(default=1000, ge=1, le=5000)
    max_results: int = Field(default=50, ge=1, le=200)
    max_file_bytes: int = Field(default=262144, ge=1, le=1048576)
    max_total_bytes: int = Field(default=5242880, ge=1, le=20971520)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        query = value.strip()
        if not query:
            raise ValueError("query must not be blank")
        return query
