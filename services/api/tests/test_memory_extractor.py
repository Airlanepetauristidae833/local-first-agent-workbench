from app.schemas.memory import MemoryKind, MemoryScope
from app.services.memory_extractor import (
    episode_memory,
    explicit_memory_candidates,
    stage_experience_memory,
)


def test_explicit_memory_candidates_require_user_authored_cues() -> None:
    candidates = explicit_memory_candidates(
        (
            "\u6211\u504f\u597d\u7528\u5bf9\u6bd4\u8868\u3002"
            "\u4ee5\u540e\u8bf7\u4e0d\u8981\u7701\u7565\u8bba\u6587 DOI\u3002"
        ),
        source="workbench",
        source_ref="run-1",
        project_id="paper",
    )
    assert {item.kind for item in candidates} == {
        MemoryKind.PREFERENCE,
        MemoryKind.CONSTRAINT,
    }
    assert all(item.scope == MemoryScope.GLOBAL for item in candidates)
    assert all(item.metadata["project_id"] == "paper" for item in candidates)


def test_ordinary_text_is_not_promoted_to_an_explicit_memory() -> None:
    assert explicit_memory_candidates(
        "Summarize this document.",
        source="openwebui",
        source_ref="chat-1",
    ) == []


def test_episode_and_stage_experience_are_project_memories() -> None:
    episode = episode_memory(
        user_text="Compare the two methods.",
        assistant_text="Method A is faster; method B is more accurate.",
        source="openwebui",
        source_ref="chat-1",
        project_id="project-1",
        conversation_id="conversation-1",
    )
    experience = stage_experience_memory(
        stage_title="Validation",
        result="All regression checks passed.",
        session_id="session-1",
        project_id="project-1",
    )
    assert episode.kind == MemoryKind.EPISODE
    assert experience.kind == MemoryKind.EXPERIENCE
    assert episode.scope == MemoryScope.PROJECT
    assert experience.scope == MemoryScope.PROJECT
    assert episode.project_id == experience.project_id == "project-1"


def test_project_episode_and_decision_do_not_leak_into_global_memory() -> None:
    episode = episode_memory(
        user_text="Compare private project alternatives.",
        assistant_text="Project-only conclusion.",
        source="workbench",
        source_ref="run-2",
        project_id="private-paper",
    )
    decision = explicit_memory_candidates(
        "\u51b3\u5b9a\uff1a\u8fd9\u7bc7\u8bba\u6587\u4f7f\u7528 APA \u683c\u5f0f\u3002",
        source="workbench",
        source_ref="run-2",
        project_id="private-paper",
    )[0]

    assert episode.scope == MemoryScope.PROJECT
    assert episode.project_id == "private-paper"
    assert decision.scope == MemoryScope.PROJECT
    assert decision.project_id == "private-paper"
