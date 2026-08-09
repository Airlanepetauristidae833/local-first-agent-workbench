from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from app.config import get_settings
from app.schemas.chat_run import ChatRun, ChatRunEvent, ChatRunStatus


class ChatRunConflictError(RuntimeError):
    """A run transition lost a race or used a stale worker attempt."""


class ChatRunNotFoundError(LookupError):
    """The requested chat run does not exist."""


class ChatRunService:
    """Durable, event-backed storage for asynchronous chat execution.

    The tables intentionally share ``Settings.agent_store_path`` with agent
    sessions while using independent names and transactions. Each mutating
    operation uses ``BEGIN IMMEDIATE`` so status changes, partial output, and
    the corresponding event sequence are committed atomically.
    """

    _ACTIVE_STATUSES: ClassVar[set[ChatRunStatus]] = {
        ChatRunStatus.QUEUED,
        ChatRunStatus.RUNNING,
    }

    def __init__(self, database_path: Path | None = None) -> None:
        self._database_path = database_path or get_settings().agent_store_path
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS chat_runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    request_message_id TEXT,
                    input_text TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
                    ),
                    partial_text TEXT NOT NULL DEFAULT '',
                    final_text TEXT,
                    error TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT UNIQUE,
                    worker_id TEXT,
                    attempt_id TEXT,
                    attempt_no INTEGER NOT NULL DEFAULT 0,
                    last_event_seq INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    cancelled_at TEXT
                    )"""
                )
                connection.execute(
                    """CREATE INDEX IF NOT EXISTS chat_runs_status_created_idx
                    ON chat_runs (status, created_at, id)"""
                )
                connection.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS
                    chat_runs_one_active_per_session_idx
                    ON chat_runs (session_id)
                    WHERE status IN ('queued', 'running')"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS chat_run_events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    UNIQUE (run_id, idempotency_key),
                    FOREIGN KEY (run_id) REFERENCES chat_runs(id) ON DELETE CASCADE
                    )"""
                )
            self._initialized = True

    def create(
        self,
        session_id: str,
        input_text: str,
        *,
        request_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> ChatRun:
        session_id = session_id.strip()
        input_text = input_text.strip()
        if not session_id:
            raise ValueError("session_id must not be blank")
        if not input_text:
            raise ValueError("input_text must not be blank")
        run_id = run_id or str(uuid4())
        created_at = self._as_utc(now or datetime.now(timezone.utc))
        metadata = dict(metadata or {})
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._find_create_duplicate(
                connection, run_id=run_id, idempotency_key=idempotency_key
            )
            if existing is not None:
                if (
                    existing.session_id != session_id
                    or existing.input_text != input_text
                    or existing.request_message_id != request_message_id
                    or existing.metadata != metadata
                ):
                    raise ChatRunConflictError(
                        "the create idempotency key was reused with different input"
                    )
                return existing
            try:
                connection.execute(
                    """INSERT INTO chat_runs (
                    id, session_id, request_message_id, input_text, status,
                    partial_text, metadata_json, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'queued', '', ?, ?, ?, ?)""",
                    (
                        run_id,
                        session_id,
                        request_message_id,
                        input_text,
                        self._json(metadata),
                        idempotency_key,
                        created_at.isoformat(),
                        created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ChatRunConflictError(
                    "the session already has an active chat run"
                ) from exc
            self._insert_event(
                connection,
                run_id,
                "run_created",
                {
                    "session_id": session_id,
                    "request_message_id": request_message_id,
                    "input_text": input_text,
                    "metadata": metadata,
                    "status": ChatRunStatus.QUEUED.value,
                },
                created_at,
                f"create:{idempotency_key or run_id}",
            )
            return self._require_in_transaction(connection, run_id)

    def get(self, run_id: str) -> ChatRun | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM chat_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def list_runs(
        self,
        *,
        statuses: set[ChatRunStatus] | None = None,
        session_id: str | None = None,
        limit: int = 10_000,
    ) -> list[ChatRun]:
        if not 1 <= limit <= 100_000:
            raise ValueError("limit must be between 1 and 100000")
        clauses: list[str] = []
        values: list[Any] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            values.extend(status.value for status in sorted(statuses, key=str))
        if session_id is not None:
            clauses.append("session_id = ?")
            values.append(session_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM chat_runs{where} ORDER BY created_at, id LIMIT ?",
                values,
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def claim(
        self,
        worker_id: str,
        *,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> ChatRun | None:
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError("worker_id must not be blank")
        claimed_at = self._as_utc(now or datetime.now(timezone.utc))
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if run_id is None:
                row = connection.execute(
                    """SELECT * FROM chat_runs
                    WHERE status = 'queued'
                    ORDER BY created_at, id LIMIT 1"""
                ).fetchone()
                if row is None:
                    return None
            else:
                row = connection.execute(
                    "SELECT * FROM chat_runs WHERE id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise ChatRunNotFoundError(f"chat run '{run_id}' does not exist")
                existing = self._run_from_row(row)
                if existing.status == ChatRunStatus.RUNNING:
                    if existing.worker_id == worker_id:
                        return existing
                    raise ChatRunConflictError("the chat run is already claimed")
                if existing.status != ChatRunStatus.QUEUED:
                    raise ChatRunConflictError(
                        f"a {existing.status.value} chat run cannot be claimed"
                    )
            selected = self._run_from_row(row)
            attempt_id = str(uuid4())
            cursor = connection.execute(
                """UPDATE chat_runs SET status = 'running', worker_id = ?,
                attempt_id = ?, attempt_no = attempt_no + 1, started_at = ?,
                completed_at = NULL, cancelled_at = NULL, error = NULL,
                updated_at = ? WHERE id = ? AND status = 'queued'""",
                (
                    worker_id,
                    attempt_id,
                    claimed_at.isoformat(),
                    claimed_at.isoformat(),
                    selected.id,
                ),
            )
            if cursor.rowcount != 1:
                raise ChatRunConflictError("the chat run was claimed concurrently")
            claimed = self._require_in_transaction(connection, selected.id)
            self._insert_event(
                connection,
                selected.id,
                "run_claimed",
                {
                    "status": ChatRunStatus.RUNNING.value,
                    "worker_id": worker_id,
                    "attempt_id": attempt_id,
                    "attempt_no": claimed.attempt_no,
                },
                claimed_at,
                f"claim:{attempt_id}",
            )
            return self._require_in_transaction(connection, selected.id)

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        partial_text: str = "",
        attempt_id: str,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ChatRunEvent:
        event_type = event_type.strip()
        if not event_type:
            raise ValueError("event_type must not be blank")
        event_at = self._as_utc(now or datetime.now(timezone.utc))
        payload = dict(payload or {})
        if partial_text:
            payload.setdefault("partial_text", partial_text)
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._event_by_key(
                connection, run_id, idempotency_key
            )
            if duplicate is not None:
                if duplicate.event_type != event_type or duplicate.payload != payload:
                    raise ChatRunConflictError(
                        "the event idempotency key was reused with different content"
                    )
                return duplicate
            run = self._require_in_transaction(connection, run_id)
            self._require_running_attempt(run, attempt_id)
            if partial_text:
                connection.execute(
                    """UPDATE chat_runs SET partial_text = partial_text || ?,
                    updated_at = ? WHERE id = ? AND status = 'running'
                    AND attempt_id = ?""",
                    (partial_text, event_at.isoformat(), run_id, attempt_id),
                )
            return self._insert_event(
                connection,
                run_id,
                event_type,
                payload,
                event_at,
                idempotency_key,
            )

    def complete(
        self,
        run_id: str,
        final_text: str | None = None,
        *,
        attempt_id: str,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ChatRun:
        completed_at = self._as_utc(now or datetime.now(timezone.utc))
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_in_transaction(connection, run_id)
            resolved_text = run.partial_text if final_text is None else final_text
            if run.status == ChatRunStatus.COMPLETED:
                if run.attempt_id == attempt_id and run.final_text == resolved_text:
                    return run
                raise ChatRunConflictError("the chat run completed with a different result")
            self._require_running_attempt(run, attempt_id)
            cursor = connection.execute(
                """UPDATE chat_runs SET status = 'completed', final_text = ?,
                partial_text = ?, error = NULL, completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND attempt_id = ?""",
                (
                    resolved_text,
                    resolved_text,
                    completed_at.isoformat(),
                    completed_at.isoformat(),
                    run_id,
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ChatRunConflictError("the chat run changed concurrently")
            self._insert_event(
                connection,
                run_id,
                "run_completed",
                {
                    "status": ChatRunStatus.COMPLETED.value,
                    "final_text": resolved_text,
                    "attempt_id": attempt_id,
                },
                completed_at,
                idempotency_key or f"complete:{attempt_id}",
            )
            return self._require_in_transaction(connection, run_id)

    def fail(
        self,
        run_id: str,
        error: str,
        *,
        attempt_id: str,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ChatRun:
        error = error.strip()
        if not error:
            raise ValueError("error must not be blank")
        failed_at = self._as_utc(now or datetime.now(timezone.utc))
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_in_transaction(connection, run_id)
            if run.status == ChatRunStatus.FAILED:
                if run.attempt_id == attempt_id and run.error == error:
                    return run
                raise ChatRunConflictError("the chat run failed with a different error")
            self._require_running_attempt(run, attempt_id)
            cursor = connection.execute(
                """UPDATE chat_runs SET status = 'failed', error = ?,
                completed_at = ?, updated_at = ? WHERE id = ?
                AND status = 'running' AND attempt_id = ?""",
                (
                    error,
                    failed_at.isoformat(),
                    failed_at.isoformat(),
                    run_id,
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ChatRunConflictError("the chat run changed concurrently")
            self._insert_event(
                connection,
                run_id,
                "run_failed",
                {
                    "status": ChatRunStatus.FAILED.value,
                    "error": error,
                    "attempt_id": attempt_id,
                },
                failed_at,
                idempotency_key or f"fail:{attempt_id}",
            )
            return self._require_in_transaction(connection, run_id)

    def cancel(
        self,
        run_id: str,
        *,
        reason: str = "cancelled by user",
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ChatRun:
        cancelled_at = self._as_utc(now or datetime.now(timezone.utc))
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._require_in_transaction(connection, run_id)
            if run.status == ChatRunStatus.CANCELLED:
                return run
            if run.status not in self._ACTIVE_STATUSES:
                raise ChatRunConflictError(
                    f"a {run.status.value} chat run cannot be cancelled"
                )
            cursor = connection.execute(
                """UPDATE chat_runs SET status = 'cancelled', error = ?,
                cancelled_at = ?, completed_at = ?, updated_at = ?
                WHERE id = ? AND status IN ('queued', 'running')""",
                (
                    reason,
                    cancelled_at.isoformat(),
                    cancelled_at.isoformat(),
                    cancelled_at.isoformat(),
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ChatRunConflictError("the chat run changed concurrently")
            self._insert_event(
                connection,
                run_id,
                "run_cancelled",
                {
                    "status": ChatRunStatus.CANCELLED.value,
                    "reason": reason,
                    "attempt_id": run.attempt_id,
                },
                cancelled_at,
                idempotency_key or f"cancel:{run_id}",
            )
            return self._require_in_transaction(connection, run_id)

    def requeue_interrupted(
        self,
        run_id: str | None = None,
        *,
        reason: str = "worker interrupted",
        now: datetime | None = None,
    ) -> list[ChatRun]:
        requeued_at = self._as_utc(now or datetime.now(timezone.utc))
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if run_id is None:
                rows = connection.execute(
                    "SELECT * FROM chat_runs WHERE status = 'running' ORDER BY created_at, id"
                ).fetchall()
            else:
                row = connection.execute(
                    "SELECT * FROM chat_runs WHERE id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise ChatRunNotFoundError(f"chat run '{run_id}' does not exist")
                rows = [row] if row["status"] == ChatRunStatus.RUNNING.value else []
            requeued: list[ChatRun] = []
            for row in rows:
                run = self._run_from_row(row)
                cursor = connection.execute(
                    """UPDATE chat_runs SET status = 'queued', worker_id = NULL,
                    attempt_id = NULL, started_at = NULL, completed_at = NULL,
                    cancelled_at = NULL, error = NULL, partial_text = '',
                    final_text = NULL, updated_at = ?
                    WHERE id = ? AND status = 'running' AND attempt_id = ?""",
                    (requeued_at.isoformat(), run.id, run.attempt_id),
                )
                if cursor.rowcount != 1:
                    continue
                self._insert_event(
                    connection,
                    run.id,
                    "run_requeued",
                    {
                        "status": ChatRunStatus.QUEUED.value,
                        "reason": reason,
                        "previous_worker_id": run.worker_id,
                        "previous_attempt_id": run.attempt_id,
                        "reset": True,
                        "previous_partial_length": len(run.partial_text),
                    },
                    requeued_at,
                    f"requeue:{run.attempt_id}",
                )
                requeued.append(self._require_in_transaction(connection, run.id))
            return requeued

    def list_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int = 1_000,
    ) -> list[ChatRunEvent]:
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        self.initialize()
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM chat_runs WHERE id = ?", (run_id,)
            ).fetchone() is None:
                raise ChatRunNotFoundError(f"chat run '{run_id}' does not exist")
            rows = connection.execute(
                """SELECT * FROM chat_run_events WHERE run_id = ? AND seq > ?
                ORDER BY seq LIMIT ?""",
                (run_id, after_seq, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def _find_create_duplicate(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        idempotency_key: str | None,
    ) -> ChatRun | None:
        clauses = ["id = ?"]
        values: list[Any] = [run_id]
        if idempotency_key is not None:
            clauses.append("idempotency_key = ?")
            values.append(idempotency_key)
        row = connection.execute(
            f"SELECT * FROM chat_runs WHERE {' OR '.join(clauses)} LIMIT 1",
            values,
        ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def _require_in_transaction(
        self, connection: sqlite3.Connection, run_id: str
    ) -> ChatRun:
        row = connection.execute(
            "SELECT * FROM chat_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ChatRunNotFoundError(f"chat run '{run_id}' does not exist")
        return self._run_from_row(row)

    @staticmethod
    def _require_running_attempt(run: ChatRun, attempt_id: str) -> None:
        if run.status != ChatRunStatus.RUNNING:
            raise ChatRunConflictError(
                f"a {run.status.value} chat run cannot accept worker output"
            )
        if not attempt_id or run.attempt_id != attempt_id:
            raise ChatRunConflictError("the worker attempt is stale")

    def _event_by_key(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        idempotency_key: str | None,
    ) -> ChatRunEvent | None:
        if idempotency_key is None:
            return None
        row = connection.execute(
            """SELECT * FROM chat_run_events
            WHERE run_id = ? AND idempotency_key = ?""",
            (run_id, idempotency_key),
        ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: datetime,
        idempotency_key: str | None,
    ) -> ChatRunEvent:
        duplicate = self._event_by_key(connection, run_id, idempotency_key)
        if duplicate is not None:
            return duplicate
        row = connection.execute(
            "SELECT last_event_seq FROM chat_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ChatRunNotFoundError(f"chat run '{run_id}' does not exist")
        seq = int(row[0]) + 1
        connection.execute(
            """INSERT INTO chat_run_events
            (run_id, seq, event_type, payload_json, idempotency_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                seq,
                event_type,
                self._json(payload),
                idempotency_key,
                created_at.isoformat(),
            ),
        )
        connection.execute(
            """UPDATE chat_runs SET last_event_seq = ?, updated_at = ?
            WHERE id = ?""",
            (seq, created_at.isoformat(), run_id),
        )
        return ChatRunEvent(
            run_id=run_id,
            seq=seq,
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
            created_at=created_at,
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> ChatRun:
        return ChatRun(
            id=row["id"],
            session_id=row["session_id"],
            request_message_id=row["request_message_id"],
            input_text=row["input_text"],
            status=row["status"],
            partial_text=row["partial_text"],
            final_text=row["final_text"],
            error=row["error"],
            metadata=json.loads(row["metadata_json"]),
            idempotency_key=row["idempotency_key"],
            worker_id=row["worker_id"],
            attempt_id=row["attempt_id"],
            attempt_no=row["attempt_no"],
            last_event_seq=row["last_event_seq"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            cancelled_at=row["cancelled_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> ChatRunEvent:
        return ChatRunEvent(
            run_id=row["run_id"],
            seq=row["seq"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
