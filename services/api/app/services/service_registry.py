from datetime import datetime, timezone
from functools import lru_cache
from threading import RLock

from app.schemas.service import ServiceInfo, ServiceStatus


class ServiceRegistry:
    def __init__(self) -> None:
        self._services: dict[str, ServiceInfo] = {}
        self._lock = RLock()

    def register(
        self,
        name: str,
        status: ServiceStatus,
        version: str | None = None,
    ) -> ServiceInfo:
        service = ServiceInfo(
            name=name,
            status=status,
            version=version,
            updated_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._services[name] = service
        return service

    def list(self) -> list[ServiceInfo]:
        with self._lock:
            return sorted(self._services.values(), key=lambda item: item.name)


@lru_cache
def get_service_registry() -> ServiceRegistry:
    return ServiceRegistry()
