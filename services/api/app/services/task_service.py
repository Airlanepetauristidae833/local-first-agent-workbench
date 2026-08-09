from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import RLock
from uuid import uuid4

from app.config import get_settings
from app.schemas.task import TaskCreate, TaskRecord, TaskStatus


class TaskService:
    def __init__(self, store_path: Path) -> None:
        self._store_path = store_path
        self._lock = RLock()

    def create(self, request: TaskCreate) -> TaskRecord:
        now = datetime.now(timezone.utc)
        task = TaskRecord(
            id=str(uuid4()),
            name=request.name,
            payload=request.payload,
            status=TaskStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            tasks = self._load()
            tasks.append(task)
            self._save(tasks)
        return task

    def list_tasks(self) -> list[TaskRecord]:
        with self._lock:
            return self._load()

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return next((task for task in self._load() if task.id == task_id), None)

    def start(self, task_id: str) -> TaskRecord:
        return self._transition(task_id, TaskStatus.RUNNING)

    def complete(self, task_id: str, result: dict) -> TaskRecord:
        return self._transition(
            task_id,
            TaskStatus.COMPLETED,
            result=result,
        )

    def fail(self, task_id: str, error: str) -> TaskRecord:
        return self._transition(
            task_id,
            TaskStatus.FAILED,
            error=error,
        )

    def fail_interrupted(self) -> list[TaskRecord]:
        with self._lock:
            tasks = self._load()
            now = datetime.now(timezone.utc)
            changed: list[TaskRecord] = []
            for index, task in enumerate(tasks):
                if task.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                    continue
                updated = task.model_copy(
                    update={
                        "status": TaskStatus.FAILED,
                        "updated_at": now,
                        "completed_at": now,
                        "error": "task interrupted by API restart",
                    }
                )
                tasks[index] = updated
                changed.append(updated)
            if changed:
                self._save(tasks)
            return changed

    def _transition(
        self,
        task_id: str,
        status: TaskStatus,
        result: dict | None = None,
        error: str | None = None,
    ) -> TaskRecord:
        with self._lock:
            tasks = self._load()
            current = next((task for task in tasks if task.id == task_id), None)
            if current is None:
                raise KeyError(f"task '{task_id}' not found")
            now = datetime.now(timezone.utc)
            updates = {
                "status": status,
                "updated_at": now,
                "result": result,
                "error": error,
            }
            if status is TaskStatus.RUNNING:
                updates["started_at"] = now
            if status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                updates["completed_at"] = now
            updated = current.model_copy(update=updates)
            tasks[tasks.index(current)] = updated
            self._save(tasks)
            return updated

    def _load(self) -> list[TaskRecord]:
        if not self._store_path.exists():
            return []
        raw = json.loads(self._store_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise RuntimeError("task store must contain a JSON array")
        return [TaskRecord.model_validate(item) for item in raw]

    def _save(self, tasks: list[TaskRecord]) -> None:
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._store_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                [task.model_dump(mode="json") for task in tasks],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._store_path)


@lru_cache
def get_task_service() -> TaskService:
    return TaskService(get_settings().task_store_path)
