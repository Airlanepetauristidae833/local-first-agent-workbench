---
type: knowledge
status: active
project:
domain: system
tags:
  - vault
  - governance
created: 2026-07-26
updated: 2026-07-26
---

# Vault Guide

## Source of truth

Markdown in this vault is the authoritative source for durable knowledge. Open WebUI
Knowledge, embeddings, vector databases, and RAG indexes are derived layers that may be
deleted and rebuilt from the vault.

## Properties

Every maintained note should include these properties whenever applicable:

```text
type
status
project
domain
tags
created
updated
```

## Lifecycle

1. Capture new ideas in `00 Inbox`.
2. Put material tied to a defined objective in `01 Projects`.
3. Distill conclusions that can be reused across projects into `03 Knowledge`.
4. Store experiments in `05 Experiments`, including their environment, procedure, and results.
5. Record significant choices in `06 Decisions`, preserving rationale and alternatives.
6. Move completed, superseded, or abandoned material to `09 Archive`.

## AI write boundaries

- Start with read-only AI access.
- Route automatically generated drafts only to `00 Inbox` or another explicitly controlled folder.
- Require human approval for deletion, renaming, and bulk edits.
- Never promote unverified model output directly into maintained knowledge.
