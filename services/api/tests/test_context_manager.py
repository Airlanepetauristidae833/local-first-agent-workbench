from datetime import datetime, timezone

from app.schemas.agent import (
    AgentMessage,
    AgentPhase,
    AgentSession,
    AgentStatus,
    KnowledgeState,
)
from app.services.context_manager import (
    ContextPolicy,
    build_context_envelope,
    estimate_tokens,
    plan_compaction,
    trim_middle_to_tokens,
    trim_to_tokens,
)


def make_session(message_count: int = 0) -> AgentSession:
    now = datetime.now(timezone.utc)
    messages = [
        AgentMessage(
            id=f"message-{index}",
            role="user" if index % 2 == 0 else "assistant",
            kind="chat",
            content=f"Conversation fact {index}: preserve decision-{index}.",
            phase=AgentPhase.IMPLEMENTATION,
            created_at=now,
        )
        for index in range(message_count)
    ]
    return AgentSession(
        id="context-test",
        title="Context test",
        goal="Produce a reliable research report with traceable sources.",
        phase=AgentPhase.IMPLEMENTATION,
        status=AgentStatus.WAITING_FOR_STAGE,
        execution_mode="local",
        local_percent=100,
        codex_percent=0,
        routing_reason="The local model can complete the work.",
        knowledge_state=KnowledgeState.AVAILABLE,
        messages=messages,
        created_at=now,
        updated_at=now,
    )


def test_token_estimator_and_trimmer_handle_multilingual_text() -> None:
    text = "English and \u4e2d\u6587 mixed context" * 100
    assert estimate_tokens(text) > 100
    trimmed = trim_to_tokens(text, 50)
    assert trimmed.endswith("…")
    assert estimate_tokens(trimmed) <= 51


def test_middle_trimmer_preserves_the_start_and_end() -> None:
    text = "BEGIN:" + ("\u4e2d" * 500) + ":END"
    trimmed = trim_middle_to_tokens(text, 80)

    assert trimmed.startswith("BEGIN:")
    assert trimmed.endswith(":END")
    assert "middle omitted" in trimmed
    assert estimate_tokens(trimmed) <= 80


def test_compaction_keeps_recent_messages_and_hashes_the_source() -> None:
    session = make_session(20)
    policy = ContextPolicy(recent_messages=8, max_messages_before_compaction=16)
    plan = plan_compaction(session, policy)
    assert plan is not None
    assert plan.through_count == 12
    assert len(plan.messages) == 12
    assert "decision-0" in plan.prompt
    assert len(plan.source_hash) == 64
    assert len(session.messages) == 20  # raw history is never deleted


def test_context_envelope_uses_summary_recent_history_and_provenance() -> None:
    session = make_session(10).model_copy(
        update={
            "rolling_summary": "User approved the research outline.",
            "compacted_message_count": 4,
            "compaction_count": 1,
        }
    )
    policy = ContextPolicy(input_budget_tokens=2_500, recent_max_tokens=600)
    envelope = build_context_envelope(
        session,
        current_message="What is the next step?",
        knowledge_context=(
            "[source=notes.md; project=paper; chunk=abc123] Verified evidence."
        ),
        memory_context="The user prefers concise evidence tables.",
        policy=policy,
    )
    assert "DURABLE CONVERSATION SUMMARY" in envelope.system_prompt
    assert "User approved the research outline" in envelope.system_prompt
    assert "RETRIEVED PROJECT EVIDENCE (UNTRUSTED)" in envelope.system_prompt
    assert "chunk=abc123" in envelope.system_prompt
    assert "SHARED LONG-TERM MEMORY" in envelope.system_prompt
    assert envelope.telemetry.memory_tokens > 0
    assert envelope.model_message == "What is the next step?"
    assert envelope.telemetry.compaction_count == 1
    assert envelope.telemetry.estimated_input_tokens <= policy.input_budget_tokens + 1


def test_context_envelope_bounds_one_oversized_user_turn() -> None:
    session = make_session(1)
    policy = ContextPolicy(
        input_budget_tokens=2_000,
        current_message_max_tokens=1_200,
        minimum_system_tokens=600,
    )
    current = "\u5f00" * 3_000 + "\u6700\u7ec8\u95ee\u9898"

    envelope = build_context_envelope(
        session,
        current_message=current,
        knowledge_context="evidence " * 2_000,
        memory_context="preference " * 1_000,
        policy=policy,
    )

    assert envelope.model_message.startswith("\u5f00")
    assert "middle omitted" in envelope.model_message
    assert envelope.model_message.endswith("\u6700\u7ec8\u95ee\u9898")
    assert envelope.telemetry.current_message_truncated is True
    assert envelope.telemetry.estimated_input_tokens <= policy.input_budget_tokens
