import sqlite3
from datetime import datetime, timezone

import pytest

from app.routers.agent import (
    _codex_handoff_result,
    _codex_stage_prompt,
    _fallback_analysis,
    _parse_analysis,
    _rag_context,
    _recent_conversation_context,
    _source_catalog,
)
from app.schemas.agent import (
    AgentPhase,
    AgentSession,
    AgentStage,
    AgentStatus,
    KnowledgeState,
)
from app.schemas.memory import MemoryCreate, MemoryKind, MemoryScope
from app.services.agent_service import (
    AgentConflictError,
    AgentDeletedError,
    AgentService,
)
from app.services.chat_run_service import ChatRunService
from app.services.external_chat_link_service import ExternalChatLinkService
from app.services.memory_service import MemoryService


def test_local_research_project_stays_local() -> None:
    decision = _fallback_analysis(
        "\u4e3a\u8bba\u6587\u68c0\u7d22\u6587\u732e\u5e76\u5f62\u6210\u5199\u4f5c\u63d0\u7eb2"
    )
    assert decision["local_percent"] == 100
    assert decision["codex_required"] is False
    proposal_ids = {
        item.id
        for item in _source_catalog(
            "\u8425\u517b\u5b66\u8bba\u6587\u7814\u7a76"
        )
    }
    assert "scholarly" in proposal_ids
    assert "official" in proposal_ids


def test_implementation_project_reserves_a_codex_stage() -> None:
    decision = _parse_analysis(
        '{"local_percent":80,"codex_required":true,"reason":"File operations are required",'
        '"stages":[{"title":"Planning","description":"Define the approach","owner":"local"}]}',
        "Build an application",
    )
    assert decision["local_percent"] == 80
    assert any(stage.owner == "codex" for stage in decision["stages"])


def test_fabricated_source_stage_is_rejected() -> None:
    decision = _parse_analysis(
        '{"local_percent":100,"codex_required":false,"reason":"Complete locally",'
        '"stages":[{"title":"Retrieval","description":"Output a fabricated source list","owner":"local"}]}',
        "Literature research",
    )
    assert decision["stages"] == []


def test_agent_session_is_persisted_with_timeline(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    session = AgentSession(
        id="session-1", title="Paper project", goal="Complete the paper outline",
        phase=AgentPhase.KNOWLEDGE,
        status=AgentStatus.AWAITING_KNOWLEDGE_APPROVAL,
        execution_mode="local", local_percent=100, codex_percent=0,
        routing_reason="The local model can complete this task",
        knowledge_state=KnowledgeState.MISSING,
        created_at=now, updated_at=now,
    )
    service = AgentService(tmp_path / "agent.sqlite3")
    session = service.append_message(session, "user", "goal", session.goal)
    saved = service.save(session)

    loaded = service.get_session(saved.id)
    assert loaded is not None
    assert loaded.messages[0].content == "Complete the paper outline"
    assert service.list_sessions()[0].id == saved.id


def test_revision_conflicts_and_tombstones_prevent_stale_overwrites(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    service = AgentService(tmp_path / "agent.sqlite3")
    saved = service.save(
        AgentSession(
            id="revision-session",
            title="Original name",
            goal="Verify concurrency protection",
            phase=AgentPhase.IMPLEMENTATION,
            status=AgentStatus.WAITING_FOR_STAGE,
            execution_mode="local",
            local_percent=100,
            codex_percent=0,
            routing_reason="Complete locally",
            knowledge_state=KnowledgeState.AVAILABLE,
            created_at=now,
            updated_at=now,
        )
    )
    first = service.get_session(saved.id)
    stale = service.get_session(saved.id)
    assert first is not None and stale is not None

    renamed = service.save(first.model_copy(update={"title": "New name"}))
    assert renamed.revision == saved.revision + 1
    with pytest.raises(AgentConflictError):
        service.save(stale.model_copy(update={"goal": "Stale overwrite"}))

    assert service.delete(saved.id, expected_revision=renamed.revision)
    with pytest.raises(AgentDeletedError):
        service.save(renamed.model_copy(update={"title": "Must not revive"}))
    assert service.get_session(saved.id) is None


def test_session_delete_cascades_derived_state_and_preserves_unrelated_data(
    tmp_path,
) -> None:
    database = tmp_path / "agent.sqlite3"
    agents = AgentService(database)
    runs = ChatRunService(database)
    links = ExternalChatLinkService(database)
    memories = MemoryService(database)
    now = datetime.now(timezone.utc)

    def session(identifier: str, project_id: str) -> AgentSession:
        return AgentSession(
            id=identifier,
            title=identifier,
            goal="Verify permanent deletion",
            project_id=project_id,
            phase=AgentPhase.IMPLEMENTATION,
            status=AgentStatus.WAITING_FOR_STAGE,
            execution_mode="local",
            local_percent=100,
            codex_percent=0,
            routing_reason="Local lifecycle test",
            knowledge_state=KnowledgeState.AVAILABLE,
            created_at=now,
            updated_at=now,
        )

    target = agents.save(session("delete-me", "paper-a"))
    other = agents.save(session("keep-me", "paper-b"))
    target_run = runs.create(target.id, "target request")
    other_run = runs.create(other.id, "other request")
    target_claim = runs.claim("test-worker", run_id=target_run.id)
    other_claim = runs.claim("test-worker", run_id=other_run.id)
    assert target_claim is not None and target_claim.attempt_id
    assert other_claim is not None and other_claim.attempt_id
    runs.append_event(
        target_run.id,
        "token",
        {"content": "target"},
        partial_text="target",
        attempt_id=target_claim.attempt_id,
    )
    runs.append_event(
        other_run.id,
        "token",
        {"content": "other"},
        partial_text="other",
        attempt_id=other_claim.attempt_id,
    )

    target_run_memory = memories.create(
        MemoryCreate(
            scope=MemoryScope.PROJECT,
            project_id="paper-a",
            kind=MemoryKind.EPISODE,
            content="Derived target run memory",
            source="workbench",
            source_ref=target_run.id,
        )
    )
    target_stage_memory = memories.create(
        MemoryCreate(
            scope=MemoryScope.PROJECT,
            project_id="paper-a",
            kind=MemoryKind.EXPERIENCE,
            content="Derived target stage memory",
            source="workbench_stage",
            source_ref=target.id,
        )
    )
    manual_global = memories.create(
        MemoryCreate(
            scope=MemoryScope.GLOBAL,
            kind=MemoryKind.PREFERENCE,
            content="Keep this hand-written global preference",
            source="user",
        )
    )
    other_memory = memories.create(
        MemoryCreate(
            scope=MemoryScope.PROJECT,
            project_id="paper-b",
            kind=MemoryKind.EPISODE,
            content="Keep the other task memory",
            source="workbench",
            source_ref=other_run.id,
        )
    )

    links.ensure_chat(
        source="open-webui",
        opaque_user_id="target-user",
        external_chat_id="target-chat",
        agent_session_id=target.id,
    )
    links.ensure_message(
        source="open-webui",
        opaque_user_id="target-user",
        external_chat_id="target-chat",
        external_message_id="target-message",
        parent_external_message_id=None,
        agent_message_id="target-agent-message",
        job_id=target_run.id,
        request_hash="a" * 64,
    )
    links.ensure_chat(
        source="open-webui",
        opaque_user_id="other-user",
        external_chat_id="other-chat",
        agent_session_id=other.id,
    )
    links.ensure_message(
        source="open-webui",
        opaque_user_id="other-user",
        external_chat_id="other-chat",
        external_message_id="other-message",
        parent_external_message_id=None,
        agent_message_id="other-agent-message",
        job_id=other_run.id,
        request_hash="b" * 64,
    )

    assert agents.delete(target.id, expected_revision=target.revision) is True

    assert agents.get_session(target.id) is None
    assert agents.get_session(other.id) is not None
    assert runs.get(target_run.id) is None
    assert runs.get(other_run.id) is not None
    assert memories.get(target_run_memory.id) is None
    assert memories.get(target_stage_memory.id) is None
    assert memories.get(manual_global.id) == manual_global
    assert memories.get(other_memory.id) == other_memory
    assert (
        links.get_chat(
            source="open-webui",
            opaque_user_id="target-user",
            external_chat_id="target-chat",
        )
        is None
    )
    assert (
        links.get_chat(
            source="open-webui",
            opaque_user_id="other-user",
            external_chat_id="other-chat",
        )
        is not None
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM chat_run_events WHERE run_id = ?",
            (target_run.id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            """SELECT COUNT(*) FROM agent_memory_terms AS terms
            LEFT JOIN agent_memories AS memory ON memory.id = terms.memory_id
            WHERE memory.id IS NULL"""
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_session_tombstones WHERE id = ?",
            (target.id,),
        ).fetchone()[0] == 1
    with pytest.raises(AgentDeletedError):
        agents.save(target)


def test_session_delete_rolls_back_every_cascade_on_failure(tmp_path) -> None:
    database = tmp_path / "agent.sqlite3"
    agents = AgentService(database)
    runs = ChatRunService(database)
    memories = MemoryService(database)
    now = datetime.now(timezone.utc)
    saved = agents.save(
        AgentSession(
            id="rollback-session",
            title="Rollback",
            goal="Verify atomic deletion",
            project_id="paper-a",
            phase=AgentPhase.IMPLEMENTATION,
            status=AgentStatus.WAITING_FOR_STAGE,
            execution_mode="local",
            local_percent=100,
            codex_percent=0,
            routing_reason="Local lifecycle test",
            knowledge_state=KnowledgeState.AVAILABLE,
            created_at=now,
            updated_at=now,
        )
    )
    run = runs.create(saved.id, "rollback request")
    memory = memories.create(
        MemoryCreate(
            scope=MemoryScope.PROJECT,
            project_id="paper-a",
            kind=MemoryKind.EPISODE,
            content="Rollback derived memory",
            source="workbench",
            source_ref=run.id,
        )
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TRIGGER reject_chat_run_delete
            BEFORE DELETE ON chat_runs BEGIN
                SELECT RAISE(ABORT, 'test rollback');
            END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        agents.delete(saved.id, expected_revision=saved.revision)

    assert agents.get_session(saved.id) is not None
    assert runs.get(run.id) is not None
    assert memories.get(memory.id) is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM agent_session_tombstones WHERE id = ?",
            (saved.id,),
        ).fetchone()[0] == 0


def test_legacy_agent_database_migrates_without_losing_sessions(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    database = tmp_path / "legacy-agent.sqlite3"
    legacy = AgentSession(
        id="legacy-session",
        title="Legacy session",
        goal="Verify migration",
        phase=AgentPhase.KNOWLEDGE,
        status=AgentStatus.AWAITING_KNOWLEDGE_APPROVAL,
        execution_mode="local",
        local_percent=100,
        codex_percent=0,
        routing_reason="Complete locally",
        knowledge_state=KnowledgeState.MISSING,
        created_at=now,
        updated_at=now,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE agent_sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            document TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO agent_sessions VALUES (?, ?, ?, ?, ?)",
            (
                legacy.id,
                legacy.title,
                legacy.model_dump_json(
                    exclude={
                        "revision",
                        "active_operation",
                        "operation_started_at",
                        "codex_context_consent",
                    }
                ),
                now.isoformat(),
                now.isoformat(),
            ),
        )

    service = AgentService(database)
    loaded = service.get_session(legacy.id)
    assert loaded is not None
    assert loaded.revision == 0
    assert loaded.codex_context_consent is False
    saved = service.save(loaded.model_copy(update={"title": "Migrated session"}))
    assert saved.revision == 1
    assert service.get_session(legacy.id).title == "Migrated session"


def test_interrupted_agent_operations_recover_to_actionable_states(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    service = AgentService(tmp_path / "agent.sqlite3")

    def session(identifier: str, **changes) -> AgentSession:
        document = AgentSession(
            id=identifier,
            title=identifier,
            goal="Recovery test",
            phase=AgentPhase.IMPLEMENTATION,
            status=AgentStatus.WAITING_FOR_STAGE,
            execution_mode="local",
            local_percent=100,
            codex_percent=0,
            routing_reason="Complete locally",
            knowledge_state=KnowledgeState.AVAILABLE,
            stages=[
                AgentStage(
                    id="stage-1",
                    title="Execution",
                    description="Complete the stage",
                    owner="local",
                )
            ],
            created_at=now,
            updated_at=now,
        )
        return service.save(document.model_copy(update=changes))

    local = session(
        "local-running",
        status=AgentStatus.LOCAL_RUNNING,
        active_operation="local-stage:0",
        operation_started_at=now,
        stages=[
            AgentStage(
                id="stage-1",
                title="Execution",
                description="Complete the stage",
                owner="local",
                status="running",
                started_at=now,
            )
        ],
    )
    building = session(
        "knowledge-building",
        phase=AgentPhase.KNOWLEDGE,
        status=AgentStatus.AWAITING_KNOWLEDGE_APPROVAL,
        knowledge_state=KnowledgeState.BUILDING,
        active_operation="knowledge_build",
        operation_started_at=now,
    )
    invalid_codex = session(
        "codex-invalid",
        status=AgentStatus.CODEX_RUNNING,
        active_operation="codex:missing-plan",
        operation_started_at=now,
        stages=[
            AgentStage(
                id="stage-1",
                title="Execution",
                description="Complete the stage",
                owner="codex",
                status="preparing",
                started_at=now,
                plan_id="missing-plan",
            )
        ],
    )
    valid_codex = session(
        "codex-valid",
        status=AgentStatus.CODEX_RUNNING,
        active_operation="codex:valid-plan",
        operation_started_at=now,
        stages=[
            AgentStage(
                id="stage-1",
                title="Execution",
                description="Complete the stage",
                owner="codex",
                status="running",
                started_at=now,
                plan_id="valid-plan",
            )
        ],
    )

    recovered = service.recover_interrupted({"valid-plan"})
    assert {item.id for item in recovered} == {
        local.id,
        building.id,
        invalid_codex.id,
    }
    loaded_local = service.get_session(local.id)
    loaded_building = service.get_session(building.id)
    loaded_invalid = service.get_session(invalid_codex.id)
    loaded_valid = service.get_session(valid_codex.id)
    assert loaded_local is not None and loaded_local.status == AgentStatus.WAITING_FOR_STAGE
    assert loaded_local.stages[0].status == "pending"
    assert loaded_building is not None and loaded_building.knowledge_state == KnowledgeState.FAILED
    assert loaded_building.active_operation is None
    assert loaded_invalid is not None and loaded_invalid.status == AgentStatus.WAITING_FOR_STAGE
    assert loaded_invalid.stages[0].plan_id is None
    assert loaded_valid is not None and loaded_valid.status == AgentStatus.CODEX_RUNNING
    assert loaded_valid.active_operation == "codex:valid-plan"


def test_chat_context_is_bounded_and_includes_rag_sources(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    session = AgentSession(
        id="chat-context",
        title="Continuous conversation",
        goal="Answer using conversation history and knowledge",
        phase=AgentPhase.IMPLEMENTATION,
        status=AgentStatus.WAITING_FOR_STAGE,
        execution_mode="local",
        local_percent=100,
        codex_percent=0,
        routing_reason="Complete locally",
        knowledge_state=KnowledgeState.AVAILABLE,
        created_at=now,
        updated_at=now,
    )
    service = AgentService(tmp_path / "agent.sqlite3")
    for index in range(14):
        session = service.append_message(
            session,
            "user" if index % 2 == 0 else "assistant",
            "chat",
            f"message-{index}",
        )
    history = _recent_conversation_context(session)
    assert "message-13" in history
    assert "message-2" in history
    assert history.startswith("user: message-2")

    rag = _rag_context(
        [
            {"source": "Projects/paper.md", "text": "Verifiable evidence"},
            {"source": "Projects/notes.md", "text": "Supporting material"},
        ]
    )
    assert "source=Projects/paper.md" in rag
    assert "project=unknown" in rag
    assert "Verifiable evidence" in rag


def test_codex_context_is_shared_only_after_explicit_consent() -> None:
    now = datetime.now(timezone.utc)
    stage = AgentStage(
        id="implementation",
        title="Implementation",
        description="Modify the project",
        owner="codex",
    )

    def make(consent: bool) -> AgentSession:
        return AgentSession(
            id=f"consent-{consent}",
            title="Context authorization",
            goal="Complete the implementation",
            project_id="paper-library",
            workspace_id="sample-workspace",
            phase=AgentPhase.IMPLEMENTATION,
            status=AgentStatus.CODEX_RUNNING,
            execution_mode="hybrid",
            codex_context_consent=consent,
            local_percent=70,
            codex_percent=30,
            routing_reason="Tool-based implementation is required",
            knowledge_state=KnowledgeState.READY,
            research_note="/vault/research.md",
            stages=[stage],
            created_at=now,
            updated_at=now,
        )

    private_result = _codex_handoff_result(
        make(False),
        stage,
        handoff_note="/vault/handoff.md",
        local_plan={"secret": "plan"},
        local_response="private analysis",
        knowledge_evidence=[{"text": "private evidence"}],
        prior_stage_results=[{"result": "private result"}],
    )
    assert private_result["context_shared"] is False
    assert "knowledge_evidence" not in private_result
    assert "prior_stage_results" not in private_result
    assert "research_note" not in private_result
    assert "host_context" not in private_result

    shared_result = _codex_handoff_result(
        make(True),
        stage,
        handoff_note="/vault/handoff.md",
        local_plan={"mode": "hybrid"},
        local_response="approved analysis",
        knowledge_evidence=[{"text": "approved evidence"}],
        prior_stage_results=[{"result": "approved result"}],
    )
    assert shared_result["context_shared"] is True
    assert shared_result["knowledge_evidence"][0]["text"] == "approved evidence"
    assert shared_result["prior_stage_results"][0]["result"] == "approved result"
    assert shared_result["host_context"]["workspace_id"] == "sample-workspace"


def test_codex_stage_prompt_respects_the_end_to_end_schema_limit() -> None:
    now = datetime.now(timezone.utc)
    session = AgentSession(
        id="bounded-codex-prompt",
        title="Boundary test",
        goal="G" * 20_000,
        phase=AgentPhase.IMPLEMENTATION,
        status=AgentStatus.WAITING_FOR_STAGE,
        execution_mode="hybrid",
        local_percent=70,
        codex_percent=30,
        routing_reason="Tool-based implementation is required",
        knowledge_state=KnowledgeState.READY,
        stages=[],
        created_at=now,
        updated_at=now,
    )
    stage = AgentStage(
        id="implementation",
        title="Implementation",
        description="Stage requirements " * 200,
        owner="codex",
    )

    prompt = _codex_stage_prompt(session, stage, "Additional context " * 5_000)

    assert 1 <= len(prompt) <= 20_000
    assert "Current stage: Implementation" in prompt
    assert "Complete only this stage and validate the result." in prompt
