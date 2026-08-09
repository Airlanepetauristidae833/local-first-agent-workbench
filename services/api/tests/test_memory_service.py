from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from app.schemas.memory import (
    MemoryCreate,
    MemoryKind,
    MemoryScope,
    MemoryUpdate,
)
from app.services.agent_service import AgentService
from app.services.memory_service import (
    MemoryConflictError,
    MemoryDuplicateError,
    MemoryService,
)


def make_memory(
    content: str,
    *,
    kind: MemoryKind = MemoryKind.FACT,
    scope: MemoryScope = MemoryScope.GLOBAL,
    project_id: str | None = None,
    source: str = "user",
    confidence: float = 1.0,
) -> MemoryCreate:
    return MemoryCreate(
        scope=scope,
        project_id=project_id,
        kind=kind,
        content=content,
        source=source,
        source_ref="session:test" if source != "user" else None,
        confidence=confidence,
        metadata={"test": True},
    )


def test_scope_validation_and_all_memory_kinds(tmp_path) -> None:
    with pytest.raises(ValidationError, match="require a project_id"):
        make_memory(
            "Project-only fact",
            scope=MemoryScope.PROJECT,
        )
    with pytest.raises(ValidationError, match="must not have a project_id"):
        make_memory(
            "Invalid global fact",
            scope=MemoryScope.GLOBAL,
            project_id="paper",
        )

    service = MemoryService(tmp_path / "personal-agent.sqlite3")
    created = [
        service.create(make_memory(f"Memory for {kind.value}", kind=kind))
        for kind in MemoryKind
    ]

    assert {item.kind for item in created} == set(MemoryKind)
    assert MemoryKind.EPISODE in {item.kind for item in service.list()}


def test_crud_uses_independent_tables_in_agent_database(tmp_path) -> None:
    database = tmp_path / "personal-agent.sqlite3"
    AgentService(database).initialize()
    service = MemoryService(database)
    created = service.create(
        make_memory(
            "Prefer concise answers with explicit evidence.",
            kind=MemoryKind.PREFERENCE,
            source="user-profile",
            confidence=0.85,
        )
    )

    assert created.scope == MemoryScope.GLOBAL
    assert created.source == "user-profile"
    assert created.source_ref == "session:test"
    assert created.confidence == 0.85
    assert created.metadata == {"test": True}
    assert service.get(created.id) == created
    assert service.list(kind=MemoryKind.PREFERENCE) == [created]
    assert service.list(min_confidence=0.9) == []

    updated = service.update(
        created.id,
        MemoryUpdate(
            content="Prefer structured, concise answers with explicit evidence.",
            confidence=0.95,
            source="confirmed-user-profile",
            source_ref="conversation:42",
            metadata={"confirmed": True},
        ),
        expected_revision=created.revision,
    )
    assert updated is not None
    assert updated.revision == created.revision + 1
    assert updated.source == "confirmed-user-profile"
    assert updated.source_ref == "conversation:42"
    assert updated.metadata == {"confirmed": True}

    with pytest.raises(MemoryConflictError):
        service.delete(updated.id, expected_revision=created.revision)
    assert service.delete(updated.id, expected_revision=updated.revision) is True
    assert service.delete(updated.id) is False
    assert service.get(updated.id) is None

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        term_count = connection.execute(
            "SELECT COUNT(*) FROM agent_memory_terms"
        ).fetchone()[0]
    assert {"agent_sessions", "agent_memories", "agent_memory_terms"} <= tables
    assert term_count == 0


def test_delete_project_memories_cascades_terms_and_preserves_other_scopes(
    tmp_path,
) -> None:
    database = tmp_path / "personal-agent.sqlite3"
    service = MemoryService(database)
    deleted_project = [
        service.create(
            make_memory(
                f"Paper A project memory {index}",
                scope=MemoryScope.PROJECT,
                project_id="paper-a",
            )
        )
        for index in range(2)
    ]
    other_project = service.create(
        make_memory(
            "Paper B project memory",
            scope=MemoryScope.PROJECT,
            project_id="paper-b",
        )
    )
    global_memory = service.create(make_memory("Shared global memory"))

    assert service.delete_project_memories("paper-a") == 2
    assert service.delete_project_memories("paper-a") == 0
    assert service.list(scope=MemoryScope.PROJECT, project_id="paper-a") == []
    assert service.get(other_project.id) == other_project
    assert service.get(global_memory.id) == global_memory

    with sqlite3.connect(database) as connection:
        deleted_term_count = connection.execute(
            """SELECT COUNT(*) FROM agent_memory_terms
            WHERE memory_id IN (?, ?)""",
            tuple(memory.id for memory in deleted_project),
        ).fetchone()[0]
        orphan_term_count = connection.execute(
            """SELECT COUNT(*) FROM agent_memory_terms AS terms
            LEFT JOIN agent_memories AS memories ON memories.id = terms.memory_id
            WHERE memories.id IS NULL"""
        ).fetchone()[0]
    assert deleted_term_count == 0
    assert orphan_term_count == 0


def test_delete_project_memories_rolls_back_the_whole_project_on_failure(
    tmp_path,
) -> None:
    database = tmp_path / "personal-agent.sqlite3"
    service = MemoryService(database)
    first = service.create(
        make_memory(
            "First protected project memory",
            scope=MemoryScope.PROJECT,
            project_id="paper-a",
        )
    )
    second = service.create(
        make_memory(
            "Second protected project memory",
            scope=MemoryScope.PROJECT,
            project_id="paper-a",
        )
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"""CREATE TRIGGER reject_project_memory_delete
            BEFORE DELETE ON agent_memories
            WHEN OLD.id = '{second.id}'
            BEGIN
                SELECT RAISE(ABORT, 'test rollback');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        service.delete_project_memories("paper-a")

    assert {memory.id for memory in service.list(
        scope=MemoryScope.PROJECT,
        project_id="paper-a",
    )} == {first.id, second.id}
    with sqlite3.connect(database) as connection:
        term_count = connection.execute(
            """SELECT COUNT(*) FROM agent_memory_terms
            WHERE memory_id IN (?, ?)""",
            (first.id, second.id),
        ).fetchone()[0]
    assert term_count > 0


def test_create_deduplicates_normalized_content_per_scope_and_kind(tmp_path) -> None:
    service = MemoryService(tmp_path / "personal-agent.sqlite3")
    first = service.create(
        make_memory(
            "Prefer concise answers.",
            kind=MemoryKind.PREFERENCE,
            confidence=0.8,
        )
    )
    duplicate = service.create(
        make_memory(
            "  PREFER   concise answers!  ",
            kind=MemoryKind.PREFERENCE,
            source="model-inference",
            confidence=0.4,
        )
    )
    different_kind = service.create(
        make_memory(
            "Prefer concise answers",
            kind=MemoryKind.CONSTRAINT,
        )
    )
    project_copy = service.create(
        make_memory(
            "Prefer concise answers",
            kind=MemoryKind.PREFERENCE,
            scope=MemoryScope.PROJECT,
            project_id="paper-a",
        )
    )

    assert duplicate.id == first.id
    assert duplicate.source == first.source
    assert different_kind.id != first.id
    assert project_copy.id != first.id
    assert len(service.list()) == 3

    with pytest.raises(MemoryDuplicateError) as duplicate_update:
        service.update(
            different_kind.id,
            MemoryUpdate(kind=MemoryKind.PREFERENCE),
        )
    assert duplicate_update.value.existing_id == first.id


def test_search_handles_english_words_bigrams_and_project_isolation(tmp_path) -> None:
    service = MemoryService(tmp_path / "personal-agent.sqlite3")
    global_memory = service.create(
        make_memory(
            "Always cite peer reviewed research papers.",
            kind=MemoryKind.PREFERENCE,
            confidence=0.9,
        )
    )
    paper_memory = service.create(
        make_memory(
            "The research paper follows APA style and reports effect sizes.",
            kind=MemoryKind.DECISION,
            scope=MemoryScope.PROJECT,
            project_id="paper-a",
        )
    )
    service.create(
        make_memory(
            "The research paper uses MLA style.",
            kind=MemoryKind.DECISION,
            scope=MemoryScope.PROJECT,
            project_id="paper-b",
        )
    )

    results = service.search("research paper", project_id="paper-a")
    ids = [result.memory.id for result in results]
    assert paper_memory.id in ids
    assert global_memory.id in ids
    assert all(result.memory.project_id != "paper-b" for result in results)
    assert "research paper" in results[0].matched_terms

    project_only = service.search(
        "research paper",
        project_id="paper-a",
        include_global=False,
    )
    assert [result.memory.id for result in project_only] == [paper_memory.id]
    assert service.search("MLA citation", project_id="paper-a") == []
    assert service.search("peer reviewed")[-1].memory.id == global_memory.id


def test_search_handles_cjk_characters_and_bigrams_and_reindexes(tmp_path) -> None:
    service = MemoryService(tmp_path / "personal-agent.sqlite3")
    memory = service.create(
        make_memory(
            (
                "\u8bba\u6587\u5199\u4f5c\u5e94\u4f18\u5148\u4f7f\u7528"
                "\u540c\u884c\u8bc4\u5ba1\u6765\u6e90\uff0c\u5e76\u4fdd\u7559"
                "\u5b8c\u6574\u5f15\u7528\u3002"
            ),
            kind=MemoryKind.CONSTRAINT,
            scope=MemoryScope.PROJECT,
            project_id="thesis",
        )
    )

    results = service.search(
        "\u8bba\u6587\u5199\u4f5c", project_id="thesis"
    )
    assert results[0].memory.id == memory.id
    assert {
        "\u8bba\u6587",
        "\u6587\u5199",
        "\u5199\u4f5c",
    } <= set(results[0].matched_terms)

    updated = service.update(
        memory.id,
        MemoryUpdate(
            content=(
                "\u6570\u636e\u5206\u6790\u5fc5\u987b\u8bb0\u5f55\u8f6f\u4ef6"
                "\u7248\u672c\u4e0e\u968f\u673a\u79cd\u5b50\u3002"
            )
        ),
    )
    assert updated is not None
    assert service.search(
        "\u8bba\u6587\u5199\u4f5c", project_id="thesis"
    ) == []
    assert service.search(
        "\u6570\u636e\u5206\u6790", project_id="thesis"
    )[0].memory.id == memory.id


def test_episode_is_shared_across_service_instances_and_sessions(tmp_path) -> None:
    database = tmp_path / "personal-agent.sqlite3"
    writer = MemoryService(database)
    episode = writer.create(
        make_memory(
            "Prior session concluded that the literature review needs a PRISMA diagram.",
            kind=MemoryKind.EPISODE,
            scope=MemoryScope.PROJECT,
            project_id="review",
            source="workbench-session-summary",
            confidence=0.88,
        )
    )

    # A fresh service represents another Workbench/Open WebUI conversation.
    reader = MemoryService(database)
    results = reader.search(
        "literature review PRISMA",
        project_id="review",
        kinds=[MemoryKind.EPISODE],
    )

    assert results[0].memory.id == episode.id
    assert results[0].memory.kind == MemoryKind.EPISODE
    assert results[0].memory.source == "workbench-session-summary"
