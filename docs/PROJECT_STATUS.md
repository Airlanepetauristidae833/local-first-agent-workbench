# Project Status

Local-First Agent Workbench is a public reference implementation for one trusted user on
one workstation. It is suitable for evaluation, self-hosting experiments, and
contributions. It is not presented as a hosted service, a multi-tenant platform, or a
hardware-independent appliance.

## Capability status

| Area | Repository status | Evidence or qualification |
| --- | --- | --- |
| Workbench UI | Implemented | Responsive FastAPI console with task, knowledge, memory, workspace, and system views |
| Durable model runs | Implemented and covered by automated tests | SQLite run/event state, atomic claims, attempt isolation, cancellation, idempotency, SSE replay, and bounded watchdog retry |
| Context management | Implemented and covered by automated tests | Independent budgets, rolling structured summaries, recent-message retention, and visible oversized-turn truncation |
| Long-term memory | Implemented and covered by automated tests | Global/project scopes, kind restrictions, term search, deduplication, pagination, revision checks, and deletion |
| Obsidian-derived RAG | Implemented and covered by lifecycle tests | Source registry, fingerprints, diagnostics, temporary rebuild, atomic swap, and provenance |
| Approved web research | Implemented; runtime availability is external | Curated SearXNG routes with an explicit user approval boundary |
| Open WebUI bridge | Implemented | Minimal authenticated `/v1` surface for `agent.personal-agent`; native Ollama remains separate |
| Optional Codex handoff | Reference implementation | Approval-gated planning plus a separately operated Windows worker and isolated execution directory |
| Local model acceleration | Environment-dependent | Ollama owns model loading and GPU/CPU placement; the repository does not claim a throughput figure |
| Private remote access | Deployment option, not bundled identity infrastructure | Ports bind to loopback by default; a private reverse proxy or VPN must own authentication and TLS |
| Multi-user or multi-node deployment | Not supported | Requires tenant-aware authorization and external transactional run/event infrastructure |

## Verification levels

The project uses explicit verification levels so public documentation does not turn a
machine-specific observation into a general claim.

### Portable release gate

The checked-in test wrappers validate Compose rendering, API behavior, knowledge-index
lifecycle, Python linting, script syntax, embedded console JavaScript, privacy constraints,
and whitespace. This is the reproducible baseline for every contribution.

### Runtime smoke gate

A maintainer must run the stack on real hardware to verify model availability, streaming,
knowledge diagnostics, search behavior, database integrity, and Open WebUI provider
configuration. Results apply only to the tested environment.

### Recovery and long-context gates

Browser disconnect, API restart, stale-attempt isolation, memory boundaries, and context
compaction have dedicated manual and automated checks. A configured threshold is not
described as a completed long-run test unless the long conversation was actually executed.

See [Acceptance and Release Gates](ACCEPTANCE.md) for the full procedures.

## Security posture

Default deployment is deliberately narrow:

- host-facing services bind to loopback;
- generated secrets and runtime data are ignored;
- Open WebUI uses a dedicated random token for the container-only Agent bridge;
- external chat identifiers are stored as source-scoped hashes;
- knowledge and web content are treated as untrusted evidence;
- the Workbench exposes no generic remote shell or arbitrary download endpoint;
- Codex access to local context requires per-project consent.

Changing the bind address is not, by itself, a secure remote-access design. Read
[Security](../SECURITY.md) before changing the network boundary.

## Known boundaries

- SQLite and the in-process execution model assume a single API instance.
- The included Codex worker is a Windows PowerShell reference, not a cross-platform daemon.
- Search quality and availability depend on third-party engines.
- Model correctness, latency, context capacity, and GPU placement depend on the selected
  Ollama model and host hardware.
- Open WebUI maintains its own native-chat history and document index; those stores are
  intentionally not merged into canonical Agent memory or Workbench RAG.
- No public hosted demo, official binary, package, benchmark result, or anonymous telemetry
  is asserted by this repository.

## Maturity and release policy

The repository publishes source, reproducible validation instructions, and versioned
release archives. A versioned release is created only after the portable gate passes on
its release commit and a maintainer records the applicable runtime gates. Hardware-
specific results belong in release notes with the tested environment identified.

The diagrams in [Guided Demonstration](DEMO.md) are repository-native explanatory assets,
not production screenshots. Example task names, allocations, and source counts should
never be presented as adoption metrics.

## Contribution priorities

Useful contribution areas, without implying a delivery commitment, include:

1. additional recovery and upgrade fixtures;
2. a portable, least-authority Codex worker design;
3. accessible and testable Workbench interaction improvements;
4. extraction and provenance support for more knowledge formats;
5. privacy-preserving operational diagnostics;
6. documented reference deployments behind authenticated private-network gateways.

Before proposing a scope expansion, preserve the core invariants: browser connections do
not own jobs, Obsidian material remains rebuildable source evidence, model routes are
explicit, and external content never grants execution authority.
