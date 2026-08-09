# Architecture

## Design goals

The Workbench is built around four properties that are easy to lose in a chat-first
prototype:

1. execution survives a browser or API disconnect;
2. conversation state and cross-session memory are different data types;
3. project knowledge is source-backed and rebuildable;
4. local models and external coding agents have explicit authority boundaries.

## Components

| Component | Responsibility | Persistent state |
| --- | --- | --- |
| FastAPI | Workbench UI, Agent state machine, durable runs, memory, compatibility bridge | SQLite under `data/api` |
| Knowledge service | Project registry, extraction, chunking, embeddings, provenance, writeback | registry and vector index under `data/knowledge` |
| Open WebUI | Authentication, native chat history, document UI, Personal Agent client | `data/open-webui` |
| SearXNG | Internal metasearch used only after the workflow allows research | rendered runtime settings only |
| Ollama | Local model runtime | host-managed model cache |
| Codex worker | Optional implementation handoffs in isolated workspaces | handoff records and execution workspace |
| Obsidian vault | Human-maintained project knowledge and durable project artifacts | Markdown source of truth |

## Run lifecycle

```text
queued
  -> running (worker + attempt ID)
       -> completed
       -> failed
       -> cancelled
       -> requeued (restart/watchdog, new attempt)
```

`chat_runs` stores the authoritative state. `chat_run_events` stores ordered observable
events. A partial response is committed as it is produced. The SSE endpoint replays events
after the caller's last sequence, then waits for new events. A disconnect never owns the
run and therefore cannot accidentally terminate it.

Each session can have at most one queued/running run. Idempotency keys prevent retry storms,
and a semantic request hash returns a conflict if a caller reuses an identity for different
content.

## Context hierarchy

The prompt builder allocates independent budgets in priority order:

1. stable global preferences/constraints/facts;
2. fixed project route and stage state;
3. recent verbatim conversation;
4. rolling structured summary;
5. project-scoped recalled memory;
6. source-labelled RAG evidence.

When an individual turn is too large, its head and tail are preserved with an explicit
middle-omission marker. Hidden truncation is not acceptable because it makes a user believe
the model saw text that never reached it.

## Memory model

`agent_memories` records:

- scope: `global` or `project`
- kind: `preference`, `constraint`, `fact`, `decision`, `experience`, or `episode`
- normalized content/hash, source/reference, confidence, metadata, revision, timestamps

`agent_memory_terms` is a compact term/bigram index. It is intentionally not a second vector
database. Project experience and episodes cannot enter global scope. Global recall is
restricted to stable preference/constraint/fact records.

Knowledge and memory are not synonyms. Obsidian/RAG holds source material; memory holds small
derived working facts that remain inspectable and deletable.

## Open WebUI bridge

The bridge exposes only:

```text
GET  /v1/models
POST /v1/chat/completions
```

It accepts a dedicated Bearer token from the Open WebUI container. Provider headers supply
chat/message identity placeholders; the gateway stores source-scoped SHA-256 hashes rather
than raw values. `agent.personal-agent` uses durable runs and canonical Agent memory. Native
Ollama model selection bypasses the bridge and remains ordinary Open WebUI chat.

## Knowledge lifecycle

Project manifests include source paths, file fingerprints, extraction limits, embedding
identity, chunking revision, timestamps, and diagnostics. Rebuild occurs in a temporary
collection and swaps atomically only after extraction and chunk-count checks succeed. A
failed rebuild preserves the previous good index.

The service reports stale, failed, truncated, unsupported, and missing-root states instead
of collapsing them into one green indicator.

## Web research

The local model first decides whether the current evidence requires fresh external data.
The workflow presents or applies the approved source route, queries the internal SearXNG
service, filters low-value/duplicate results, retains provenance, writes a research note to
the managed project area, and reindexes it before continuing.

SearXNG loads a small explicit engine set. General discovery uses Bing; scholarly discovery
uses OpenAlex/Crossref/arXiv before Bing fallback; source-repository lookup uses GitHub.

## Deletion and recovery

- Permanent task deletion transactionally removes its run, events, external mappings, and
  source-linked Agent memories, then leaves a tombstone against late replay.
- Knowledge-project deletion first succeeds in the knowledge service and then removes
  project-scoped memories. This is a controlled two-step cross-service operation, not a
  distributed transaction.
- API startup requeues interrupted chat runs with a new attempt.
- A first-token or stream-idle timeout retries once; the second failure is explicit.
- Backups are conservative and must not automatically delete user content.

## Mature-agent design comparison

The design follows established patterns without requiring their runtime dependencies:

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence):
  checkpointed state, recoverable execution, thread state, and cross-thread stores.
- [LangGraph memory](https://docs.langchain.com/oss/python/concepts/memory): separate
  short-term/thread and long-term/cross-session memory.
- [Letta context hierarchy](https://docs.letta.com/guides/core-concepts/memory/context-hierarchy):
  small important state in context, larger material in archival/external retrieval.
- [OpenAI streaming events](https://platform.openai.com/docs/api-reference/responses-streaming/response/output_item):
  explicit stream event types and compaction items as application-visible state.

For one trusted user, a single API instance and SQLite provide a smaller failure and backup
surface. They are not a substitute for tenant isolation or distributed coordination.
