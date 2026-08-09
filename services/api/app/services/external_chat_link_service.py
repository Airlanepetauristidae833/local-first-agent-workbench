from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings


class ExternalChatLinkConflictError(RuntimeError):
    """An opaque external identifier was reused for different work."""


@dataclass(frozen=True, slots=True)
class ExternalChatLink:
    source: str
    opaque_user_hash: str
    external_chat_hash: str
    agent_session_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExternalMessageLink:
    source: str
    opaque_user_hash: str
    external_chat_hash: str
    external_message_hash: str
    parent_external_message_hash: str | None
    agent_message_id: str
    job_id: str
    request_hash: str
    created_at: datetime


class ExternalChatLinkService:
    """Durably map external chat IDs without retaining their raw values.

    Open WebUI identifiers are opaque, but hashing them before persistence
    prevents a database export from becoming a cross-system identifier list.
    The source name remains explicit so future bridges cannot collide.
    """

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
                    """CREATE TABLE IF NOT EXISTS external_chat_links (
                    source TEXT NOT NULL,
                    opaque_user_hash TEXT NOT NULL,
                    external_chat_hash TEXT NOT NULL,
                    agent_session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source, opaque_user_hash, external_chat_hash),
                    UNIQUE (agent_session_id)
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS external_message_links (
                    source TEXT NOT NULL,
                    opaque_user_hash TEXT NOT NULL,
                    external_chat_hash TEXT NOT NULL,
                    external_message_hash TEXT NOT NULL,
                    parent_external_message_hash TEXT NOT NULL DEFAULT '',
                    agent_message_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (
                        source, opaque_user_hash, external_chat_hash,
                        external_message_hash
                    ),
                    UNIQUE (job_id)
                    )"""
                )
                connection.execute(
                    """CREATE INDEX IF NOT EXISTS external_message_links_job_idx
                    ON external_message_links (job_id)"""
                )
            self._initialized = True

    @staticmethod
    def identifier_hash(source: str, value: str) -> str:
        source = source.strip().casefold()
        value = value.strip()
        if not source or not value:
            raise ValueError("source and external identifier must not be blank")
        return hashlib.sha256(
            f"{source}\0{value}".encode("utf-8")
        ).hexdigest()

    def get_chat(
        self,
        *,
        source: str,
        opaque_user_id: str,
        external_chat_id: str,
    ) -> ExternalChatLink | None:
        source, user_hash, chat_hash = self._chat_key(
            source, opaque_user_id, external_chat_id
        )
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM external_chat_links
                WHERE source = ? AND opaque_user_hash = ?
                    AND external_chat_hash = ?""",
                (source, user_hash, chat_hash),
            ).fetchone()
        return self._chat_from_row(row) if row is not None else None

    def ensure_chat(
        self,
        *,
        source: str,
        opaque_user_id: str,
        external_chat_id: str,
        agent_session_id: str,
        now: datetime | None = None,
    ) -> ExternalChatLink:
        source, user_hash, chat_hash = self._chat_key(
            source, opaque_user_id, external_chat_id
        )
        agent_session_id = agent_session_id.strip()
        if not agent_session_id:
            raise ValueError("agent_session_id must not be blank")
        timestamp = self._as_utc(now or datetime.now(timezone.utc))
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM external_chat_links
                WHERE source = ? AND opaque_user_hash = ?
                    AND external_chat_hash = ?""",
                (source, user_hash, chat_hash),
            ).fetchone()
            if row is not None:
                existing = self._chat_from_row(row)
                if existing.agent_session_id != agent_session_id:
                    raise ExternalChatLinkConflictError(
                        "the external chat is already linked to another session"
                    )
                connection.execute(
                    """UPDATE external_chat_links SET updated_at = ?
                    WHERE source = ? AND opaque_user_hash = ?
                        AND external_chat_hash = ?""",
                    (timestamp.isoformat(), source, user_hash, chat_hash),
                )
            else:
                try:
                    connection.execute(
                        """INSERT INTO external_chat_links (
                        source, opaque_user_hash, external_chat_hash,
                        agent_session_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            source,
                            user_hash,
                            chat_hash,
                            agent_session_id,
                            timestamp.isoformat(),
                            timestamp.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ExternalChatLinkConflictError(
                        "the agent session is linked to another external chat"
                    ) from exc
            result = connection.execute(
                """SELECT * FROM external_chat_links
                WHERE source = ? AND opaque_user_hash = ?
                    AND external_chat_hash = ?""",
                (source, user_hash, chat_hash),
            ).fetchone()
        if result is None:  # pragma: no cover - guarded by insert/update
            raise RuntimeError("external chat link could not be read back")
        return self._chat_from_row(result)

    def get_message(
        self,
        *,
        source: str,
        opaque_user_id: str,
        external_chat_id: str,
        external_message_id: str,
    ) -> ExternalMessageLink | None:
        source, user_hash, chat_hash = self._chat_key(
            source, opaque_user_id, external_chat_id
        )
        message_hash = self.identifier_hash(source, external_message_id)
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM external_message_links
                WHERE source = ? AND opaque_user_hash = ?
                    AND external_chat_hash = ? AND external_message_hash = ?""",
                (source, user_hash, chat_hash, message_hash),
            ).fetchone()
        return self._message_from_row(row) if row is not None else None

    def ensure_message(
        self,
        *,
        source: str,
        opaque_user_id: str,
        external_chat_id: str,
        external_message_id: str,
        parent_external_message_id: str | None,
        agent_message_id: str,
        job_id: str,
        request_hash: str,
        now: datetime | None = None,
    ) -> ExternalMessageLink:
        source, user_hash, chat_hash = self._chat_key(
            source, opaque_user_id, external_chat_id
        )
        message_hash = self.identifier_hash(source, external_message_id)
        parent_hash = (
            self.identifier_hash(source, parent_external_message_id)
            if parent_external_message_id and parent_external_message_id.strip()
            else ""
        )
        agent_message_id = agent_message_id.strip()
        job_id = job_id.strip()
        request_hash = request_hash.strip().casefold()
        if not agent_message_id or not job_id:
            raise ValueError("agent_message_id and job_id must not be blank")
        if len(request_hash) != 64 or any(
            character not in "0123456789abcdef" for character in request_hash
        ):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        timestamp = self._as_utc(now or datetime.now(timezone.utc))
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM external_message_links
                WHERE source = ? AND opaque_user_hash = ?
                    AND external_chat_hash = ? AND external_message_hash = ?""",
                (source, user_hash, chat_hash, message_hash),
            ).fetchone()
            if row is not None:
                existing = self._message_from_row(row)
                if (
                    existing.parent_external_message_hash != (parent_hash or None)
                    or existing.agent_message_id != agent_message_id
                    or existing.job_id != job_id
                    or existing.request_hash != request_hash
                ):
                    raise ExternalChatLinkConflictError(
                        "the external message identifier was reused with different input"
                    )
                return existing
            try:
                connection.execute(
                    """INSERT INTO external_message_links (
                    source, opaque_user_hash, external_chat_hash,
                    external_message_hash, parent_external_message_hash,
                    agent_message_id, job_id, request_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source,
                        user_hash,
                        chat_hash,
                        message_hash,
                        parent_hash,
                        agent_message_id,
                        job_id,
                        request_hash,
                        timestamp.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ExternalChatLinkConflictError(
                    "the external message conflicts with an existing bridge job"
                ) from exc
            result = connection.execute(
                """SELECT * FROM external_message_links
                WHERE source = ? AND opaque_user_hash = ?
                    AND external_chat_hash = ? AND external_message_hash = ?""",
                (source, user_hash, chat_hash, message_hash),
            ).fetchone()
        if result is None:  # pragma: no cover - guarded by insert
            raise RuntimeError("external message link could not be read back")
        return self._message_from_row(result)

    @classmethod
    def _chat_key(
        cls, source: str, opaque_user_id: str, external_chat_id: str
    ) -> tuple[str, str, str]:
        source = source.strip().casefold()
        if not source:
            raise ValueError("source must not be blank")
        return (
            source,
            cls.identifier_hash(source, opaque_user_id),
            cls.identifier_hash(source, external_chat_id),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _chat_from_row(row: sqlite3.Row) -> ExternalChatLink:
        return ExternalChatLink(
            source=str(row["source"]),
            opaque_user_hash=str(row["opaque_user_hash"]),
            external_chat_hash=str(row["external_chat_hash"]),
            agent_session_id=str(row["agent_session_id"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> ExternalMessageLink:
        parent_hash = str(row["parent_external_message_hash"])
        return ExternalMessageLink(
            source=str(row["source"]),
            opaque_user_hash=str(row["opaque_user_hash"]),
            external_chat_hash=str(row["external_chat_hash"]),
            external_message_hash=str(row["external_message_hash"]),
            parent_external_message_hash=parent_hash or None,
            agent_message_id=str(row["agent_message_id"]),
            job_id=str(row["job_id"]),
            request_hash=str(row["request_hash"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection
