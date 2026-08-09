from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from app.schemas.orchestration import ExecutionPlan, RouteRequest

CODEX_LEASE_SECONDS = 120


class OrchestrationService:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY, prompt TEXT NOT NULL, document TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def create_plan(self, request: RouteRequest) -> ExecutionPlan:
        intent = self._classify(request.prompt)
        connectors = ["local_model"]
        if request.workspace_id:
            connectors.append("workspace_readonly")
        # These are permissions. The local model makes the actual per-task decision at execution time.
        needs_web = request.allow_online
        needs_codex = request.allow_codex
        if needs_web:
            connectors.append("web_research")
        if needs_codex:
            connectors.append("codex_handoff")
        authority = "read_only" if connectors in (["local_model"], ["local_model", "workspace_readonly"]) else "approval_gated"
        now = datetime.now(timezone.utc)
        plan = ExecutionPlan(
            id=str(uuid4()), prompt=request.prompt, intent=intent,
            local_workspace_id=request.workspace_id, project_id=request.project_id, connectors=connectors,
            authority=authority, approval_required=True,
            summary=self._summary(intent, connectors, request.workspace_id),
            status="planned", created_at=now, updated_at=now,
        )
        self._save(plan)
        return plan

    def list_plans(self) -> list[ExecutionPlan]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute("SELECT document FROM plans ORDER BY created_at DESC").fetchall()
        return [ExecutionPlan.model_validate_json(row[0]) for row in rows]

    def list_handoffs(self) -> list[ExecutionPlan]:
        return [
            plan
            for plan in self.list_plans()
            if plan.status in {"handoff_pending", "codex_running"}
        ]

    def get_plan(self, plan_id: str) -> ExecutionPlan | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT document FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return ExecutionPlan.model_validate_json(row[0]) if row else None

    def approve(self, plan_id: str) -> ExecutionPlan:
        plan = self._require(plan_id)
        if plan.status != "planned":
            raise ValueError("only planned requests can be approved")
        return self._replace(plan, status="approved")

    def complete(self, plan_id: str, result: dict) -> ExecutionPlan:
        plan = self._require(plan_id)
        if plan.status != "approved":
            raise ValueError("an approved plan is required")
        return self._replace(plan, status="completed", result=result)

    def handoff(self, plan_id: str, result: dict) -> ExecutionPlan:
        plan = self._require(plan_id)
        if plan.status != "approved":
            raise ValueError("an approved plan is required")
        return self._replace(plan, status="handoff_pending", result=result)

    def start_handoff(
        self,
        plan_id: str,
        worker_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = CODEX_LEASE_SECONDS,
    ) -> ExecutionPlan:
        self.initialize()
        claimed_at = self._as_utc(now or datetime.now(timezone.utc))
        lease_seconds = max(1, int(lease_seconds))
        # Claim under an IMMEDIATE transaction so a pending handoff is assigned
        # once, and an expired lease can be taken over with a fresh attempt ID.
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = self._require_in_transaction(connection, plan_id)
            if plan.status == "codex_running" and not self._lease_expired(
                plan, claimed_at
            ):
                raise ValueError("Codex handoff already has an active lease")
            if plan.status not in {"handoff_pending", "codex_running"}:
                raise ValueError("only pending Codex handoffs can be claimed")
            result = dict(plan.result or {})
            result.update(
                outcome="codex_running",
                worker_id=worker_id,
                attempt_id=str(uuid4()),
                attempt_no=int(result.get("attempt_no") or 0) + 1,
                codex_started_at=claimed_at.isoformat(),
                heartbeat_at=claimed_at.isoformat(),
                lease_expires_at=(
                    claimed_at + timedelta(seconds=lease_seconds)
                ).isoformat(),
            )
            updated = plan.model_copy(
                update={
                    "status": "codex_running",
                    "result": result,
                    "updated_at": claimed_at,
                }
            )
            self._write_in_transaction(connection, updated)
            return updated

    def heartbeat_handoff(
        self,
        plan_id: str,
        worker_id: str,
        attempt_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = CODEX_LEASE_SECONDS,
    ) -> ExecutionPlan:
        self.initialize()
        heartbeat_at = self._as_utc(now or datetime.now(timezone.utc))
        lease_seconds = max(1, int(lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = self._require_in_transaction(connection, plan_id)
            # A late duplicate heartbeat from the winning attempt is harmless.
            if plan.status in {"completed", "failed"}:
                self._require_attempt(plan, worker_id, attempt_id)
                return plan
            if plan.status != "codex_running":
                raise ValueError("a running Codex handoff is required")
            self._require_attempt(plan, worker_id, attempt_id)
            if self._lease_expired(plan, heartbeat_at):
                raise ValueError("Codex handoff lease expired")
            result = dict(plan.result or {})
            result.update(
                heartbeat_at=heartbeat_at.isoformat(),
                lease_expires_at=(
                    heartbeat_at + timedelta(seconds=lease_seconds)
                ).isoformat(),
            )
            updated = plan.model_copy(
                update={
                    "result": result,
                    "updated_at": heartbeat_at,
                }
            )
            self._write_in_transaction(connection, updated)
            return updated

    def finish_handoff(
        self,
        plan_id: str,
        execution: dict,
        *,
        now: datetime | None = None,
    ) -> ExecutionPlan:
        self.initialize()
        completed_at = self._as_utc(now or datetime.now(timezone.utc))
        worker_id = str(execution.get("worker_id") or "")
        attempt_id = str(execution.get("attempt_id") or "")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            plan = self._require_in_transaction(connection, plan_id)
            if plan.status in {"completed", "failed"}:
                self._require_attempt(plan, worker_id, attempt_id)
                return plan
            if plan.status != "codex_running":
                raise ValueError("a running Codex handoff is required")
            self._require_attempt(plan, worker_id, attempt_id)
            if self._lease_expired(plan, completed_at):
                raise ValueError("Codex handoff lease expired")
            result = dict(plan.result or {})
            result.update(
                outcome=(
                    "codex_completed" if execution.get("success") else "codex_failed"
                ),
                codex_execution=execution,
                codex_completed_at=completed_at.isoformat(),
                lease_expires_at=completed_at.isoformat(),
            )
            updated = plan.model_copy(
                update={
                    "status": "completed" if execution.get("success") else "failed",
                    "result": result,
                    "updated_at": completed_at,
                }
            )
            self._write_in_transaction(connection, updated)
            return updated

    def _replace(self, plan: ExecutionPlan, **changes: object) -> ExecutionPlan:
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_in_transaction(connection, plan.id)
            if (
                current.status != plan.status
                or current.updated_at != plan.updated_at
            ):
                raise ValueError("execution plan changed concurrently")
            updated = current.model_copy(
                update={**changes, "updated_at": datetime.now(timezone.utc)}
            )
            self._write_in_transaction(connection, updated)
            return updated

    def _save(self, plan: ExecutionPlan) -> None:
        self.initialize()
        document = plan.model_dump_json()
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO plans
                    (id, prompt, document, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        plan.id,
                        plan.prompt,
                        document,
                        plan.created_at.isoformat(),
                        plan.updated_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("execution plan already exists") from exc

    def _require(self, plan_id: str) -> ExecutionPlan:
        plan = self.get_plan(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        return plan

    @staticmethod
    def _require_in_transaction(
        connection: sqlite3.Connection, plan_id: str
    ) -> ExecutionPlan:
        row = connection.execute(
            "SELECT document FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return ExecutionPlan.model_validate_json(row[0])

    @staticmethod
    def _write_in_transaction(
        connection: sqlite3.Connection, plan: ExecutionPlan
    ) -> None:
        cursor = connection.execute(
            "UPDATE plans SET document = ?, updated_at = ? WHERE id = ?",
            (plan.model_dump_json(), plan.updated_at.isoformat(), plan.id),
        )
        if cursor.rowcount != 1:
            raise KeyError(plan.id)

    @staticmethod
    def _require_attempt(
        plan: ExecutionPlan, worker_id: str, attempt_id: str
    ) -> None:
        result = plan.result or {}
        if (
            not worker_id
            or not attempt_id
            or result.get("worker_id") != worker_id
            or result.get("attempt_id") != attempt_id
        ):
            raise ValueError("Codex handoff is owned by a different attempt")

    @classmethod
    def _lease_expired(cls, plan: ExecutionPlan, now: datetime) -> bool:
        raw_deadline = (plan.result or {}).get("lease_expires_at")
        if not raw_deadline:
            return True
        try:
            deadline = cls._as_utc(datetime.fromisoformat(str(raw_deadline)))
        except ValueError:
            return True
        return deadline <= cls._as_utc(now)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _classify(prompt: str) -> str:
        text = prompt.lower()
        if any(
            word in text
            for word in (
                "code",
                "programming",
                "script",
                "fix",
                "file",
                "bug",
                "implement",
                "\u4ee3\u7801",
                "\u7f16\u7a0b",
                "\u811a\u672c",
                "\u4fee\u590d",
                "\u6587\u4ef6",
            )
        ):
            return "project_execution"
        if any(
            word in text
            for word in (
                "research",
                "paper",
                "literature",
                "regulation",
                "latest",
                "\u7814\u7a76",
                "\u8bba\u6587",
                "\u6cd5\u89c4",
                "\u6700\u65b0",
            )
        ):
            return "research"
        return "local_assistance"

    @staticmethod
    def _needs_current_information(prompt: str) -> bool:
        return any(
            word in prompt.lower()
            for word in (
                "current",
                "latest",
                "today",
                "news",
                "regulation",
                "price",
                "online",
                "internet",
                "web",
                "search",
                "insufficient sources",
                "\u6700\u65b0",
                "\u4eca\u5929",
                "\u65b0\u95fb",
                "\u6cd5\u89c4",
                "\u4ef7\u683c",
                "\u8054\u7f51",
                "\u7f51\u7edc",
                "\u641c\u7d22",
                "\u68c0\u7d22",
                "\u8d44\u6599\u4e0d\u8db3",
            )
        )

    @staticmethod
    def _needs_execution(prompt: str) -> bool:
        return any(
            word in prompt.lower()
            for word in (
                "code",
                "programming",
                "script",
                "fix",
                "create file",
                "modify file",
                "bug",
                "implement",
                "\u4ee3\u7801",
                "\u7f16\u7a0b",
                "\u811a\u672c",
                "\u4fee\u590d",
                "\u521b\u5efa\u6587\u4ef6",
                "\u4fee\u6539\u6587\u4ef6",
            )
        )

    @staticmethod
    def _summary(intent: str, connectors: list[str], workspace_id: str | None) -> str:
        parts = ["Use the local model"]
        if workspace_id:
            parts.append(f"read bounded evidence from workspace '{workspace_id}'")
        if "web_research" in connectors:
            parts.append("request cited external research through a configured connector")
        if "codex_handoff" in connectors:
            parts.append("create an approval-gated Codex execution handoff")
        return f"{intent}: " + "; then ".join(parts) + "."
