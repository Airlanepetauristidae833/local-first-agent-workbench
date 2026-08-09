from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.schemas.agent import (
    AgentMessage,
    AgentPhase,
    AgentSession,
    AgentStatus,
    KnowledgeState,
)


class AgentConflictError(RuntimeError):
    """The session changed after the caller read it."""


class AgentDeletedError(RuntimeError):
    """A deleted session must never be recreated by a stale request."""


class AgentService:
    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                document TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0
                )"""
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(agent_sessions)")
            }
            if "revision" not in columns:
                connection.execute(
                    "ALTER TABLE agent_sessions ADD COLUMN revision INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_session_tombstones (
                id TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL
                )"""
            )

    def list_sessions(self, include_archived: bool = True) -> list[AgentSession]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document, revision FROM agent_sessions ORDER BY updated_at DESC"
            ).fetchall()
        sessions = [
            AgentSession.model_validate_json(row[0]).model_copy(
                update={"revision": row[1]}
            )
            for row in rows
        ]
        if include_archived:
            return sessions
        return [session for session in sessions if session.archived_at is None]

    def get_session(self, session_id: str) -> AgentSession | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document, revision FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return AgentSession.model_validate_json(row[0]).model_copy(
            update={"revision": row[1]}
        )

    def save(self, session: AgentSession) -> AgentSession:
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM agent_sessions WHERE id = ?", (session.id,)
            ).fetchone()
            if row is None:
                tombstone = connection.execute(
                    "SELECT 1 FROM agent_session_tombstones WHERE id = ?", (session.id,)
                ).fetchone()
                if tombstone is not None or session.revision != 0:
                    raise AgentDeletedError(
                        f"agent session '{session.id}' was deleted"
                    )
                next_revision = 1
                updated = session.model_copy(
                    update={
                        "revision": next_revision,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
                connection.execute(
                    """INSERT INTO agent_sessions
                    (id, title, document, created_at, updated_at, revision)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        updated.id,
                        updated.title,
                        updated.model_dump_json(),
                        updated.created_at.isoformat(),
                        updated.updated_at.isoformat(),
                        updated.revision,
                    ),
                )
                return updated
            current_revision = int(row[0])
            if current_revision != session.revision:
                raise AgentConflictError(
                    f"agent session '{session.id}' changed concurrently"
                )
            updated = session.model_copy(
                update={
                    "revision": current_revision + 1,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            cursor = connection.execute(
                """UPDATE agent_sessions
                SET title = ?, document = ?, updated_at = ?, revision = ?
                WHERE id = ? AND revision = ?""",
                (
                    updated.title,
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    updated.revision,
                    updated.id,
                    current_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise AgentConflictError(
                    f"agent session '{session.id}' changed concurrently"
                )
        return updated

    def mutate(
        self,
        session_id: str,
        update: Callable[[AgentSession], AgentSession],
        *,
        attempts: int = 5,
    ) -> AgentSession:
        for _ in range(attempts):
            current = self.get_session(session_id)
            if current is None:
                raise AgentDeletedError(f"agent session '{session_id}' was deleted")
            try:
                return self.save(update(current))
            except AgentConflictError:
                continue
        raise AgentConflictError(
            f"agent session '{session_id}' changed too frequently"
        )

    def delete(self, session_id: str, expected_revision: int | None = None) -> bool:
        """Permanently delete a session and every record derived from it.

        Agent sessions, durable chat runs, bridge mappings, and extracted
        memories share one SQLite database but intentionally have independent
        service classes.  Deletion therefore happens here in one write
        transaction so a failure cannot leave a partially deleted task.  The
        table checks keep deletion compatible with databases created before the
        durable chat and memory features existed.
        """

        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return False
            current_revision = int(row[0])
            if expected_revision is not None and current_revision != expected_revision:
                raise AgentConflictError(
                    f"agent session '{session_id}' changed concurrently"
                )

            has_runs = self._table_exists(connection, "chat_runs")
            has_events = self._table_exists(connection, "chat_run_events")
            has_memories = self._table_exists(connection, "agent_memories")
            has_memory_terms = self._table_exists(
                connection, "agent_memory_terms"
            )
            has_external_chats = self._table_exists(
                connection, "external_chat_links"
            )
            has_external_messages = self._table_exists(
                connection, "external_message_links"
            )

            memory_source_predicate = "source_ref = ?"
            memory_source_parameters: tuple[str, ...] = (session_id,)
            if has_runs:
                memory_source_predicate += (
                    " OR source_ref IN "
                    "(SELECT id FROM chat_runs WHERE session_id = ?)"
                )
                memory_source_parameters += (session_id,)
            if has_memories:
                if has_memory_terms:
                    connection.execute(
                        f"""DELETE FROM agent_memory_terms
                        WHERE memory_id IN (
                            SELECT id FROM agent_memories
                            WHERE {memory_source_predicate}
                        )""",
                        memory_source_parameters,
                    )
                connection.execute(
                    f"""DELETE FROM agent_memories
                    WHERE {memory_source_predicate}""",
                    memory_source_parameters,
                )

            if has_external_messages:
                external_predicates: list[str] = []
                external_parameters: list[str] = []
                if has_runs:
                    external_predicates.append(
                        "job_id IN (SELECT id FROM chat_runs WHERE session_id = ?)"
                    )
                    external_parameters.append(session_id)
                if has_external_chats:
                    external_predicates.append(
                        """EXISTS (
                            SELECT 1 FROM external_chat_links AS chat
                            WHERE chat.agent_session_id = ?
                              AND chat.source = external_message_links.source
                              AND chat.opaque_user_hash =
                                  external_message_links.opaque_user_hash
                              AND chat.external_chat_hash =
                                  external_message_links.external_chat_hash
                        )"""
                    )
                    external_parameters.append(session_id)
                if external_predicates:
                    connection.execute(
                        "DELETE FROM external_message_links WHERE "
                        + " OR ".join(external_predicates),
                        external_parameters,
                    )
            if has_external_chats:
                connection.execute(
                    "DELETE FROM external_chat_links WHERE agent_session_id = ?",
                    (session_id,),
                )

            if has_runs:
                if has_events:
                    connection.execute(
                        """DELETE FROM chat_run_events
                        WHERE run_id IN (
                            SELECT id FROM chat_runs WHERE session_id = ?
                        )""",
                        (session_id,),
                    )
                connection.execute(
                    "DELETE FROM chat_runs WHERE session_id = ?",
                    (session_id,),
                )

            cursor = connection.execute(
                "DELETE FROM agent_sessions WHERE id = ? AND revision = ?",
                (session_id, current_revision),
            )
            connection.execute(
                """INSERT OR REPLACE INTO agent_session_tombstones (id, deleted_at)
                VALUES (?, ?)""",
                (session_id, datetime.now(timezone.utc).isoformat()),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )

    def recover_interrupted(
        self,
        valid_codex_plan_ids: set[str],
        valid_chat_run_ids: set[str] | None = None,
    ) -> list[AgentSession]:
        valid_chat_run_ids = valid_chat_run_ids or set()
        recovered: list[AgentSession] = []
        for session in self.list_sessions(include_archived=True):
            reason: str | None = None
            changes: dict = {"active_operation": None, "operation_started_at": None}
            stages = list(session.stages)
            if session.knowledge_state == KnowledgeState.BUILDING:
                reason = (
                    "Knowledge-base construction was interrupted by an API "
                    "restart and has returned to the approval stage."
                )
                changes.update(
                    phase=AgentPhase.KNOWLEDGE,
                    status=AgentStatus.AWAITING_KNOWLEDGE_APPROVAL,
                    knowledge_state=KnowledgeState.FAILED,
                )
            elif session.status == AgentStatus.LOCAL_RUNNING:
                reason = (
                    "The local stage was interrupted by an API restart and "
                    "has been reset for retry."
                )
                changes.update(
                    phase=AgentPhase.IMPLEMENTATION,
                    status=AgentStatus.WAITING_FOR_STAGE,
                )
                stages = self._reset_current_stage(stages, session.current_stage_index)
                changes["stages"] = stages
            elif session.status == AgentStatus.CODEX_RUNNING:
                stage = (
                    stages[session.current_stage_index]
                    if session.current_stage_index < len(stages)
                    else None
                )
                if stage is None or not stage.plan_id or stage.plan_id not in valid_codex_plan_ids:
                    reason = (
                        "The Codex handoff did not produce a valid job before "
                        "the API restart and has been reset for retry."
                    )
                    changes.update(
                        phase=AgentPhase.IMPLEMENTATION,
                        status=AgentStatus.WAITING_FOR_STAGE,
                    )
                    stages = self._reset_current_stage(
                        stages, session.current_stage_index, clear_plan=True
                    )
                    changes["stages"] = stages
            elif (
                session.active_operation
                and session.active_operation.startswith("chat-run:")
                and session.active_operation.removeprefix("chat-run:")
                in valid_chat_run_ids
            ):
                continue
            elif session.active_operation:
                reason = (
                    "The chat request was interrupted by an API restart. Its "
                    "session lock has been released, so it can be sent again."
                )
            if reason is None:
                continue
            updated = session.model_copy(update=changes)
            updated = self.append_message(
                updated,
                "assistant",
                "recovery",
                reason,
                updated.phase,
            )
            try:
                recovered.append(self.save(updated))
            except (AgentConflictError, AgentDeletedError):
                continue
        return recovered

    @staticmethod
    def _reset_current_stage(
        stages: list,
        index: int,
        *,
        clear_plan: bool = False,
    ) -> list:
        if index >= len(stages):
            return stages
        stage = stages[index]
        changes = {
            "status": "pending",
            "started_at": None,
            "completed_at": None,
        }
        if clear_plan:
            changes["plan_id"] = None
        stages[index] = stage.model_copy(update=changes)
        return stages

    def append_message(
        self,
        session: AgentSession,
        role: str,
        kind: str,
        content: str,
        phase: AgentPhase | None = None,
        metadata: dict | None = None,
        message_id: str | None = None,
    ) -> AgentSession:
        message = AgentMessage(
            id=message_id or str(uuid4()),
            role=role,
            kind=kind,
            content=content,
            phase=phase or session.phase,
            created_at=datetime.now(timezone.utc),
            metadata=metadata or {},
        )
        return session.model_copy(update={"messages": [*session.messages, message]})

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
