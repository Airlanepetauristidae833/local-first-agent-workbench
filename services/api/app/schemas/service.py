from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class ServiceStatus(str, Enum):
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STOPPED = "stopped"


class HealthResponse(BaseModel):
    status: str


class ServiceInfo(BaseModel):
    name: str
    status: ServiceStatus
    version: str | None = None
    updated_at: datetime


class ServiceListResponse(BaseModel):
    items: list[ServiceInfo]
    count: int
