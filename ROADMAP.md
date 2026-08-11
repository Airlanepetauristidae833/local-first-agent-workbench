# Roadmap

This roadmap describes direction, not a delivery schedule. Priorities may change with
maintainer capacity, user evidence, security findings, and upstream dependencies. An item
is not committed until it is assigned to a milestone or an accepted pull request.

## Current baseline

The repository provides a reproducible, single-user and single-node reference stack with:

- a FastAPI project workbench and durable, replayable Agent runs;
- explicit local-model and optional Codex routing;
- inspectable long-term memory and bounded context compaction;
- Obsidian-derived knowledge indexing with provenance and atomic rebuilds;
- approved web research through curated SearXNG routes;
- native Ollama and Personal Agent paths in Open WebUI; and
- loopback-first Compose deployment with an optional private remote-access boundary.

The immediate goal is to make this baseline easy for an independent contributor to
understand, reproduce, validate, and improve.

## Near-term priorities

### Reproducible adoption

- Publish versioned releases with concise upgrade and rollback notes.
- Add privacy-safe screenshots or demonstrations of the Workbench and durable reconnect
  flow.
- Expand tested-environment reporting without claiming unperformed hardware tests.
- Turn repeated setup questions into troubleshooting documentation and regression tests.

### Operability and recovery

- Improve redacted diagnostics for service health, index state, durable runs, and provider
  reconciliation.
- Exercise backup, restore, interrupted-run recovery, and long-context behavior on more
  host configurations.
- Keep upgrades explicit, pinned, reversible, and safe for persisted state.

### Knowledge quality

- Improve provenance inspection, stale-source explanations, and failed-file recovery.
- Evaluate retrieval quality with synthetic, shareable fixtures and repeatable measures.
- Preserve the rule that Obsidian and source files remain authoritative while indexes are
  derived and rebuildable.

### Contributor experience and security

- Maintain clear issue, contribution, support, governance, and release paths.
- Automate dependency review and publish actionable security/update guidance.
- Keep the public privacy scanner fail-closed as the repository surface grows.

## Later candidates

These ideas require design evidence before implementation:

- additional local model or embedding adapters behind explicit capability contracts;
- a portable alternative to the Windows-only optional Codex handoff worker;
- import/export for non-secret Agent state with schema-version and rollback handling;
- stronger observability that remains local and does not introduce mandatory telemetry;
- documented extension points for project stages, retrieval sources, and approval policies;
- multi-device interaction through a private authenticated boundary.

## Deliberate non-goals

The roadmap does not currently target:

- a hosted multi-tenant SaaS;
- direct public exposure of Ollama, FastAPI, Open WebUI, SearXNG, or state files;
- a generic remote shell or arbitrary file-download service;
- silent tool execution, knowledge ingestion, or external research without the documented
  authority and approval boundaries;
- replacement of Obsidian/source documents with an opaque vector database; or
- guaranteed support for every model, accelerator, proxy, or VPN configuration.

## Proposing a roadmap change

Open a [feature request](https://github.com/Joviei/local-first-agent-workbench/issues/new?template=feature_request.yml)
that explains the user problem, evidence, alternatives, security and privacy effects, and
how success can be validated. Maintainers prioritize demonstrated recurring needs over
feature count.
