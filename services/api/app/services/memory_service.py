from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.schemas.memory import (
    MemoryCreate,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
    MemorySearchResult,
    MemoryUpdate,
)

_CJK_SEQUENCE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_ENGLISH_WORD = re.compile(r"[a-z0-9]+(?:['_-][a-z0-9]+)*")
_MAX_QUERY_TERMS = 256


class MemoryConflictError(RuntimeError):
    """The memory changed after the caller read it."""


class MemoryDuplicateError(RuntimeError):
    """An update would duplicate another canonical memory."""

    def __init__(self, existing_id: str) -> None:
        self.existing_id = existing_id
        super().__init__(f"memory duplicates existing memory '{existing_id}'")


class MemoryService:
    """Durable, project-aware memory stored beside the personal-agent state."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_memories (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL CHECK (scope IN ('global', 'project')),
                project_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL CHECK (kind IN (
                    'preference', 'constraint', 'fact', 'decision',
                    'experience', 'episode'
                )),
                content TEXT NOT NULL,
                normalized_content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                source TEXT NOT NULL,
                source_ref TEXT,
                confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                metadata TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (scope, project_id, kind, content_hash)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS agent_memory_terms (
                memory_id TEXT NOT NULL,
                term TEXT NOT NULL,
                PRIMARY KEY (memory_id, term),
                FOREIGN KEY (memory_id) REFERENCES agent_memories(id)
                    ON DELETE CASCADE
                )"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_agent_memories_scope_project
                ON agent_memories (scope, project_id, kind, updated_at DESC)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_agent_memory_terms_term
                ON agent_memory_terms (term, memory_id)"""
            )

    def create(self, memory: MemoryCreate) -> MemoryRecord:
        self.initialize()
        normalized = self._normalize(memory.content)
        content_hash = self._content_hash(normalized)
        project_key = memory.project_id or ""
        now = datetime.now(timezone.utc).isoformat()
        memory_id = str(uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT * FROM agent_memories
                WHERE scope = ? AND project_id = ? AND kind = ?
                    AND content_hash = ?""",
                (memory.scope.value, project_key, memory.kind.value, content_hash),
            ).fetchone()
            if existing is not None:
                return self._row_to_memory(existing)
            try:
                connection.execute(
                    """INSERT INTO agent_memories (
                    id, scope, project_id, kind, content, normalized_content,
                    content_hash, source, source_ref, confidence, metadata,
                    revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        memory_id,
                        memory.scope.value,
                        project_key,
                        memory.kind.value,
                        memory.content,
                        normalized,
                        content_hash,
                        memory.source,
                        memory.source_ref,
                        memory.confidence,
                        self._dump_metadata(memory.metadata),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """SELECT * FROM agent_memories
                    WHERE scope = ? AND project_id = ? AND kind = ?
                        AND content_hash = ?""",
                    (memory.scope.value, project_key, memory.kind.value, content_hash),
                ).fetchone()
                if existing is None:
                    raise
                return self._row_to_memory(existing)
            self._replace_terms(connection, memory_id, memory.content)
            row = connection.execute(
                "SELECT * FROM agent_memories WHERE id = ?", (memory_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - guarded by the successful insert
            raise RuntimeError("created memory could not be read back")
        return self._row_to_memory(row)

    def get(self, memory_id: str) -> MemoryRecord | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return self._row_to_memory(row) if row is not None else None

    def list(
        self,
        *,
        scope: MemoryScope | None = None,
        project_id: str | None = None,
        kind: MemoryKind | None = None,
        source: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        self.initialize()
        scope = MemoryScope(scope) if scope is not None else None
        kind = MemoryKind(kind) if kind is not None else None
        self._validate_filters(
            scope=scope,
            project_id=project_id,
            min_confidence=min_confidence,
            limit=limit,
            offset=offset,
        )
        clauses = ["confidence >= ?"]
        parameters: list[object] = [min_confidence]
        if scope is not None:
            clauses.append("scope = ?")
            parameters.append(scope.value)
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(project_id)
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(kind.value)
        if source is not None:
            clauses.append("source = ?")
            parameters.append(source.strip())
        parameters.extend((limit, offset))
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM agent_memories
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, id ASC LIMIT ? OFFSET ?""",
                parameters,
            ).fetchall()
        return [self._row_to_memory(row) for row in rows]

    def search(
        self,
        query: str,
        *,
        scope: MemoryScope | None = None,
        project_id: str | None = None,
        include_global: bool = True,
        kinds: Iterable[MemoryKind] | None = None,
        min_confidence: float = 0.0,
        limit: int = 20,
        offset: int = 0,
    ) -> list[MemorySearchResult]:
        self.initialize()
        query = query.strip()
        if not query:
            raise ValueError("query must not be blank")
        if len(query) > 2_000:
            raise ValueError("query must be at most 2000 characters")
        scope = MemoryScope(scope) if scope is not None else None
        self._validate_filters(
            scope=scope,
            project_id=project_id,
            min_confidence=min_confidence,
            limit=limit,
            offset=offset,
        )
        kind_values = sorted({MemoryKind(kind).value for kind in kinds or []})
        query_terms = self._terms(query)
        if not query_terms:
            return []
        ordered_terms = sorted(
            query_terms,
            key=lambda term: (-self._term_weight(term), term),
        )[:_MAX_QUERY_TERMS]
        clauses = [
            f"t.term IN ({','.join('?' for _ in ordered_terms)})",
            "m.confidence >= ?",
        ]
        parameters: list[object] = [*ordered_terms, min_confidence]
        if scope == MemoryScope.GLOBAL:
            clauses.append("m.scope = 'global'")
        elif scope == MemoryScope.PROJECT:
            clauses.extend(("m.scope = 'project'", "m.project_id = ?"))
            parameters.append(project_id)
        elif project_id is not None and include_global:
            clauses.append(
                "((m.scope = 'project' AND m.project_id = ?) OR m.scope = 'global')"
            )
            parameters.append(project_id)
        elif project_id is not None:
            clauses.extend(("m.scope = 'project'", "m.project_id = ?"))
            parameters.append(project_id)
        else:
            # Never leak one project's memories into an unscoped retrieval.
            clauses.append("m.scope = 'global'")
        if kind_values:
            clauses.append(
                f"m.kind IN ({','.join('?' for _ in kind_values)})"
            )
            parameters.extend(kind_values)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT m.* FROM agent_memories AS m
                JOIN agent_memory_terms AS t ON t.memory_id = m.id
                WHERE {' AND '.join(clauses)}""",
                parameters,
            ).fetchall()
        normalized_query = self._normalize(query)
        total_weight = sum(self._term_weight(term) for term in ordered_terms)
        results: list[MemorySearchResult] = []
        for row in rows:
            memory = self._row_to_memory(row)
            memory_terms = self._terms(memory.content)
            matched = set(ordered_terms).intersection(memory_terms)
            matched_weight = sum(self._term_weight(term) for term in matched)
            coverage = matched_weight / total_weight if total_weight else 0.0
            phrase_bonus = (
                0.35
                if normalized_query
                and normalized_query in str(row["normalized_content"])
                else 0.0
            )
            project_bonus = (
                0.1
                if project_id is not None and memory.project_id == project_id
                else 0.0
            )
            confidence_factor = 0.7 + (0.3 * memory.confidence)
            score = round(
                (coverage + phrase_bonus + project_bonus) * confidence_factor,
                6,
            )
            results.append(
                MemorySearchResult(
                    memory=memory,
                    score=score,
                    matched_terms=sorted(
                        {self._display_term(term) for term in matched}
                    ),
                )
            )
        results.sort(
            key=lambda item: (
                -item.score,
                -item.memory.updated_at.timestamp(),
                item.memory.id,
            )
        )
        return results[offset : offset + limit]

    def update(
        self,
        memory_id: str,
        update: MemoryUpdate,
        *,
        expected_revision: int | None = None,
    ) -> MemoryRecord | None:
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM agent_memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                return None
            current = self._row_to_memory(row)
            if expected_revision is not None and current.revision != expected_revision:
                raise MemoryConflictError(
                    f"memory '{memory_id}' changed concurrently"
                )
            changes = update.model_dump(exclude_unset=True)
            if changes.get("scope") == MemoryScope.GLOBAL and "project_id" not in changes:
                changes["project_id"] = None
            candidate_data = current.model_dump()
            candidate_data.update(changes)
            candidate = MemoryCreate.model_validate(
                {
                    "scope": candidate_data["scope"],
                    "project_id": candidate_data["project_id"],
                    "kind": candidate_data["kind"],
                    "content": candidate_data["content"],
                    "source": candidate_data["source"],
                    "source_ref": candidate_data["source_ref"],
                    "confidence": candidate_data["confidence"],
                    "metadata": candidate_data["metadata"] or {},
                }
            )
            normalized = self._normalize(candidate.content)
            content_hash = self._content_hash(normalized)
            project_key = candidate.project_id or ""
            duplicate = connection.execute(
                """SELECT id FROM agent_memories
                WHERE scope = ? AND project_id = ? AND kind = ?
                    AND content_hash = ? AND id <> ?""",
                (
                    candidate.scope.value,
                    project_key,
                    candidate.kind.value,
                    content_hash,
                    memory_id,
                ),
            ).fetchone()
            if duplicate is not None:
                raise MemoryDuplicateError(str(duplicate["id"]))
            next_revision = current.revision + 1
            updated_at = datetime.now(timezone.utc).isoformat()
            cursor = connection.execute(
                """UPDATE agent_memories SET
                scope = ?, project_id = ?, kind = ?, content = ?,
                normalized_content = ?, content_hash = ?, source = ?,
                source_ref = ?, confidence = ?, metadata = ?, revision = ?,
                updated_at = ?
                WHERE id = ? AND revision = ?""",
                (
                    candidate.scope.value,
                    project_key,
                    candidate.kind.value,
                    candidate.content,
                    normalized,
                    content_hash,
                    candidate.source,
                    candidate.source_ref,
                    candidate.confidence,
                    self._dump_metadata(candidate.metadata),
                    next_revision,
                    updated_at,
                    memory_id,
                    current.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise MemoryConflictError(
                    f"memory '{memory_id}' changed concurrently"
                )
            self._replace_terms(connection, memory_id, candidate.content)
            updated = connection.execute(
                "SELECT * FROM agent_memories WHERE id = ?", (memory_id,)
            ).fetchone()
        if updated is None:  # pragma: no cover - guarded by the successful update
            raise RuntimeError("updated memory could not be read back")
        return self._row_to_memory(updated)

    def delete(
        self,
        memory_id: str,
        *,
        expected_revision: int | None = None,
    ) -> bool:
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM agent_memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                return False
            revision = int(row["revision"])
            if expected_revision is not None and revision != expected_revision:
                raise MemoryConflictError(
                    f"memory '{memory_id}' changed concurrently"
                )
            cursor = connection.execute(
                "DELETE FROM agent_memories WHERE id = ? AND revision = ?",
                (memory_id, revision),
            )
        return cursor.rowcount == 1

    def delete_project_memories(self, project_id: str) -> int:
        """Delete every memory owned by a deleted project in one transaction.

        ``agent_memory_terms`` rows are removed by the table's foreign-key
        cascade.  Global memories and memories owned by other projects are
        deliberately outside the delete predicate.
        """
        self.initialize()
        project_id = project_id.strip()
        if not project_id:
            raise ValueError("project_id must not be blank")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """DELETE FROM agent_memories
                WHERE scope = 'project' AND project_id = ?""",
                (project_id,),
            )
        return cursor.rowcount

    @staticmethod
    def _validate_filters(
        *,
        scope: MemoryScope | None,
        project_id: str | None,
        min_confidence: float,
        limit: int,
        offset: int,
    ) -> None:
        if scope == MemoryScope.PROJECT and project_id is None:
            raise ValueError("project scope requires project_id")
        if scope == MemoryScope.GLOBAL and project_id is not None:
            raise ValueError("global scope must not include project_id")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("offset must not be negative")

    @staticmethod
    def _normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        characters: list[str] = []
        for character in value:
            category = unicodedata.category(character)
            if character.isspace() or category.startswith(("P", "S", "C")):
                characters.append(" ")
            else:
                characters.append(character)
        return " ".join("".join(characters).split())

    @classmethod
    def _terms(cls, value: str) -> set[str]:
        normalized = cls._normalize(value)
        terms: set[str] = set()
        words = _ENGLISH_WORD.findall(normalized)
        terms.update(f"w:{word}" for word in words)
        terms.update(
            f"wb:{left} {right}" for left, right in zip(words, words[1:])
        )
        for sequence in _CJK_SEQUENCE.findall(normalized):
            terms.update(f"c:{character}" for character in sequence)
            terms.update(
                f"cb:{sequence[index:index + 2]}"
                for index in range(len(sequence) - 1)
            )
        return terms

    @staticmethod
    def _term_weight(term: str) -> float:
        if term.startswith("wb:"):
            return 3.0
        if term.startswith("cb:"):
            return 2.5
        if term.startswith("w:"):
            return 1.5
        return 0.4

    @staticmethod
    def _display_term(term: str) -> str:
        return term.split(":", 1)[1]

    @staticmethod
    def _content_hash(normalized: str) -> str:
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _dump_metadata(metadata: dict) -> str:
        return json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=str(row["id"]),
            scope=str(row["scope"]),
            project_id=str(row["project_id"]) or None,
            kind=str(row["kind"]),
            content=str(row["content"]),
            source=str(row["source"]),
            source_ref=(
                str(row["source_ref"]) if row["source_ref"] is not None else None
            ),
            confidence=float(row["confidence"]),
            metadata=json.loads(str(row["metadata"])),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _replace_terms(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
        content: str,
    ) -> None:
        connection.execute(
            "DELETE FROM agent_memory_terms WHERE memory_id = ?", (memory_id,)
        )
        connection.executemany(
            "INSERT INTO agent_memory_terms (memory_id, term) VALUES (?, ?)",
            ((memory_id, term) for term in sorted(self._terms(content))),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
