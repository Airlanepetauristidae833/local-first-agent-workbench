from pathlib import Path

CONSOLE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "static"
    / "console.html"
).read_text(encoding="utf-8")


def test_console_exposes_long_term_memory_crud_and_search() -> None:
    assert "['memories','◆','Memory']" in CONSOLE
    assert "api(memoryListPath())" in CONSOLE
    assert "params.set('scope','project')" in CONSOLE
    assert "api('/memories/search?'" in CONSOLE
    assert "offset:String(state.memoryOffset)" in CONSOLE
    assert "MEMORY_PAGE_SIZE+1" in CONSOLE
    assert "data-action=\"memory-prev\"" in CONSOLE
    assert "data-action=\"memory-next\"" in CONSOLE
    assert "if(state.memoryKind!=='all')params.append('kinds',state.memoryKind)" in CONSOLE
    assert "method:'POST'" in CONSOLE
    assert "confirm-edit-memory" in CONSOLE
    assert "confirm-delete-memory" in CONSOLE
    assert "expected_revision=${memory.revision}" in CONSOLE
    assert "c.memory_tokens||0" in CONSOLE
    assert "api('/memory" not in CONSOLE


def test_console_uses_durable_chat_sse_contract_only() -> None:
    assert "`/agent/sessions/${sessionId}/chat-runs`" in CONSOLE
    assert "new EventSource(url)" in CONSOLE
    assert "after_seq=${state.chatLastEventId}" in CONSOLE
    assert "event.lastEventId" in CONSOLE
    assert "Last-Event-ID" in CONSOLE
    assert "run_requeued" in CONSOLE
    assert "state.chatPartial=''" in CONSOLE
    assert "seq<=state.chatLastEventId||state.chatSeenEvents.has(key)" in CONSOLE
    assert "`/agent/chat-runs/${runId}/cancel`" in CONSOLE
    assert "active_operation" in CONSOLE
    assert "resumeSessionChatRun(session)" in CONSOLE
    assert "/agent/sessions/${state.currentSession.id}/messages" not in CONSOLE


def test_console_supports_per_message_memory_suppression() -> None:
    assert 'id="chat-suppress-memory"' in CONSOLE
    assert "Do not update long-term memory from this message" in CONSOLE
    assert "The project conversation will still be retained" in CONSOLE
    assert "suppress_memory:suppressMemory" in CONSOLE
    assert "state.chatSuppressMemory=false;input.value=''" in CONSOLE
    assert "state.chatPendingKey=null" in CONSOLE


def test_console_persists_context_truncation_warning_without_fake_token_count() -> None:
    assert "'context_ready'" in CONSOLE
    assert "payload.current_message_truncated" in CONSOLE
    assert "payload.current_message_tokens" in CONSOLE
    assert 'id="chat-context-warning"' in CONSOLE
    assert "Long input truncated by the context policy" in CONSOLE
    assert "Only the beginning and end of this message" in CONSOLE
    assert "estimated tokens" in CONSOLE
    assert "chatReplayFloor" in CONSOLE
    assert "Received ${state.chatCharacterCount} characters" in CONSOLE
    assert "live token" not in CONSOLE.lower()


def test_console_route_serves_the_new_workbench(client) -> None:
    response = client.get("/console")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Long-term memory" in response.text
    assert "Do not update long-term memory from this message" in response.text
    assert "Long input truncated by the context policy" in response.text
    assert "new EventSource(url)" in response.text
    assert "/agent/chat-runs/${runId}/cancel" in response.text
