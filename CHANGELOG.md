# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned

- Broader hardware and operating-system validation from community deployments.
- Additional model-provider adapters that preserve the existing trust boundaries.

## [1.0.0] - 2026-08-11

### Added

- A responsive FastAPI workbench for project analysis, knowledge approval, staged
  execution, long-term memory, and durable chat runs.
- Obsidian-derived project RAG with provenance, atomic index replacement, and explicit
  diagnostics.
- Local Ollama inference, curated SearXNG research, an Open WebUI bridge, and an optional
  bounded Codex handoff worker.
- Reconnectable Server-Sent Events, attempt isolation, cancellation, restart recovery,
  watchdogs, and automatic rolling context summaries.
- Cross-platform bootstrap, start, stop, status, and acceptance scripts.
- A fail-closed public manifest, privacy scanner, security guidance, architecture notes,
  and automated release validation.

### Security

- Loopback-first host bindings and container-only service boundaries.
- Dedicated bridge authentication and source-scoped hashing of external identifiers.
- Test environments pin `pytest` 9.0.3 or newer to avoid vulnerable temporary-directory
  handling in earlier releases.
- Release checks that reject secrets, runtime data, personal paths, private network
  identifiers, archives, binary artifacts, and unlisted files.

[Unreleased]: https://github.com/Joviei/local-first-agent-workbench/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Joviei/local-first-agent-workbench/releases/tag/v1.0.0
