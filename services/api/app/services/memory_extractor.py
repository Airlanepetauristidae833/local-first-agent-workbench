from __future__ import annotations

import re

from app.schemas.memory import MemoryCreate, MemoryKind, MemoryScope
from app.services.context_manager import trim_to_tokens

_EXPLICIT_PATTERNS: tuple[tuple[MemoryKind, re.Pattern[str]], ...] = (
    (
        MemoryKind.PREFERENCE,
        re.compile(
            r"(?:^|[\u3002.!?\uff1b;]\s*)(?:"
            r"\u6211(?:\u66f4)?(?:\u559c\u6b22|\u504f\u597d|\u4e60\u60ef|\u5e0c\u671b)|"
            r"I\s+(?:strongly\s+)?(?:prefer|like|want)|"
            r"my\s+(?:preference|habit)\s+is)\s*[:\uff1a,\uff0c]?\s*"
            r"([^\u3002.!?\uff1b;\n]+)",
            re.I,
        ),
    ),
    (
        MemoryKind.CONSTRAINT,
        re.compile(
            r"(?:^|[\u3002.!?\uff1b;]\s*)(?:"
            r"\u4ee5\u540e(?:\u8bf7)?(?:\u4e0d\u8981|\u5fc5\u987b|\u603b\u662f|\u90fd)|"
            r"(?:please\s+)?(?:always|never|must)|"
            r"from\s+now\s+on(?:,?\s+please)?)\s*[:\uff1a,\uff0c]?\s*"
            r"([^\u3002.!?\uff1b;\n]+)",
            re.I,
        ),
    ),
    (
        MemoryKind.DECISION,
        re.compile(
            r"(?:^|[\u3002.!?\uff1b;]\s*)(?:"
            r"\u8bb0\u4f4f|\u8bf7\u8bb0\u4f4f|\u51b3\u5b9a|\u786e\u5b9a|"
            r"remember(?:\s+that)?|"
            r"please\s+remember(?:\s+that)?|we\s+(?:decided|agreed)|"
            r"the\s+decision\s+is)\s*[:\uff1a,\uff0c]?\s*"
            r"([^\u3002.!?\uff1b;\n]+)",
            re.I,
        ),
    ),
)


def explicit_memory_candidates(
    text: str,
    *,
    source: str,
    source_ref: str,
    project_id: str | None = None,
) -> list[MemoryCreate]:
    """Extract only explicit user-authored memory statements.

    The extractor intentionally avoids model-generated guesses. Every stored
    candidate is an exact substring of the user message and remains traceable
    to its source conversation.
    """

    candidates: list[MemoryCreate] = []
    seen: set[str] = set()
    for kind, pattern in _EXPLICIT_PATTERNS:
        for match in pattern.finditer(text):
            content = match.group(0).strip(" \t\r\n\u3002.!?\uff1b;")
            content = trim_to_tokens(content, 240)
            normalized = content.casefold()
            if len(content) < 4 or normalized in seen:
                continue
            seen.add(normalized)
            is_project_decision = (
                kind == MemoryKind.DECISION and project_id is not None
            )
            candidates.append(
                MemoryCreate(
                    scope=(
                        MemoryScope.PROJECT
                        if is_project_decision
                        else MemoryScope.GLOBAL
                    ),
                    project_id=project_id if is_project_decision else None,
                    kind=kind,
                    content=content,
                    source=source,
                    source_ref=source_ref,
                    confidence=1.0,
                    metadata={
                        "project_id": project_id,
                        "extraction": "explicit_pattern",
                    },
                )
            )
    return candidates[:5]


def episode_memory(
    *,
    user_text: str,
    assistant_text: str,
    source: str,
    source_ref: str,
    project_id: str,
    conversation_id: str | None = None,
) -> MemoryCreate:
    project_id = project_id.strip()
    if not project_id:
        raise ValueError("automatic episode memory requires a project_id")
    return MemoryCreate(
        scope=MemoryScope.PROJECT,
        project_id=project_id,
        kind=MemoryKind.EPISODE,
        content=(
            "User request: "
            + trim_to_tokens(user_text.strip(), 280)
            + "\nAgent result: "
            + trim_to_tokens(assistant_text.strip(), 520)
        ),
        source=source,
        source_ref=source_ref,
        confidence=0.8,
        metadata={
            "project_id": project_id,
            "conversation_id": conversation_id,
            "channel": source,
        },
    )


def stage_experience_memory(
    *,
    stage_title: str,
    result: str,
    session_id: str,
    project_id: str,
) -> MemoryCreate:
    project_id = project_id.strip()
    if not project_id:
        raise ValueError("automatic stage experience requires a project_id")
    return MemoryCreate(
        scope=MemoryScope.PROJECT,
        project_id=project_id,
        kind=MemoryKind.EXPERIENCE,
        content=(
            f"Completed stage: {trim_to_tokens(stage_title, 120)}\n"
            f"Verified result: {trim_to_tokens(result, 700)}"
        ),
        source="workbench_stage",
        source_ref=session_id,
        confidence=0.9,
        metadata={"project_id": project_id},
    )
