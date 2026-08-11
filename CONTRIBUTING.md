# Contributing

Thank you for helping improve Local-First Agent Workbench. The project is a
self-hosted reference implementation for one trusted user on one trusted node. Good
contributions preserve that understandable, privacy-conscious boundary while making the
system easier to operate, test, and adapt.

## Before you start

- Search existing issues before opening a new one.
- Use the structured bug, feature, or configuration-help issue form when it fits.
- Discuss broad architectural changes in an issue before investing in an implementation.
- Report vulnerabilities and sensitive findings privately as described in
  [SECURITY.md](SECURITY.md). Never place secrets, private hostnames, personal knowledge,
  database contents, or authentication material in an issue or pull request.

Small documentation fixes and narrowly scoped bug fixes may go directly to a pull
request.

## Development setup

The supported bootstrap path uses Docker Compose and the platform scripts. It requires
Docker with Compose v2, Python 3.11 or newer, a reachable Ollama instance, and an installed
chat model.

On Windows:

```powershell
Copy-Item .env.example .env
.\scripts\bootstrap.ps1
.\scripts\start.ps1
```

On Linux or macOS:

```bash
cp .env.example .env
./scripts/bootstrap.sh
./scripts/start.sh
```

Review `.env` before starting. Generated state belongs under the ignored runtime paths;
never commit `.env`, generated secrets, databases, logs, model data, knowledge vaults, or
local workspace contents. The optional Codex handoff worker is currently a separately
started Windows PowerShell reference.

For component responsibilities, state transitions, and trust boundaries, read
[Architecture](docs/ARCHITECTURE.md). For startup and recovery procedures, read
[Operations](docs/OPERATIONS.md).

## Design expectations

Changes should maintain these properties unless an accepted design issue explicitly
changes the project boundary:

- A browser or SSE disconnect must not own or terminate durable work.
- Retries must preserve idempotency and attempt isolation.
- Conversation history, long-term memory, and source-backed knowledge remain distinct.
- Knowledge and web content are untrusted evidence, not permission to execute tools.
- Local-model and Codex authority is explicit and visible to the user.
- Host ports remain loopback-only by default; public exposure is not a supported shortcut.
- Destructive operations are deliberate, scoped, and recoverable where practical.
- Failures, truncation, stale indexes, and degraded external services are reported rather
  than hidden behind a successful status.

## Making a change

1. Create a focused branch from the current `main` branch.
2. Keep the change narrowly scoped and avoid unrelated formatting or generated files.
3. Add or update tests for observable behavior.
4. Update user-facing documentation when configuration, operation, recovery, or security
   behavior changes.
5. Add every intentional public file to `public-manifest.txt`. The release scanner rejects
   unlisted files by design.
6. Run the release validation gate.

On Windows:

```powershell
.\scripts\test.ps1
```

On Linux or macOS:

```bash
./scripts/test.sh
```

The gate renders Compose configuration, validates source syntax, runs API and knowledge
tests, runs Ruff, performs the fail-closed privacy scan, and checks whitespace. See
[Acceptance and Release Gates](docs/ACCEPTANCE.md) for the machine-specific smoke tests
that automation cannot claim.

If the complete gate cannot run on your machine, run the relevant checks you can, explain
the limitation in the pull request, and let CI provide the portable release result. Do not
describe a hardware, model, browser, or remote-access test as passed unless it was actually
performed.

## Pull requests

A reviewable pull request should:

- explain the user-visible problem and the chosen solution;
- link the relevant issue when one exists;
- identify persistence, migration, privacy, networking, and authority-boundary effects;
- list automated and manual validation with exact results;
- include only synthetic, redacted diagnostics and fixtures; and
- remain compatible with the project's documented single-user, single-node scope.

Maintainers may ask for a smaller change, additional tests, documentation, or a design
discussion. A passing CI run is necessary but does not by itself guarantee acceptance.

By submitting a contribution, you agree that it may be distributed under the repository's
[MIT License](LICENSE).

## Community standards

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md). Support is
best-effort and is described in [SUPPORT.md](SUPPORT.md). Project roles and decision-making
are described in [GOVERNANCE.md](GOVERNANCE.md).
