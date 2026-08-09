# Acceptance and Release Gates

This document separates code-level reproducibility from machine-specific hardware and
remote-access checks.

## Automated release gate

Run one platform wrapper:

```powershell
.\scripts\test.ps1
```

```bash
./scripts/test.sh
```

The gate must fail if any step fails:

1. Docker Compose renders from `.env.example`-compatible values.
2. API regression tests pass.
3. Knowledge lifecycle tests pass.
4. Ruff reports no E4/E7/E9/F/import-order error.
5. PowerShell, Bash, Python, and embedded console JavaScript syntax checks pass.
6. The privacy scanner observes a non-empty explicit manifest and no extra files.
7. No runtime, secret, database, cache, archive, symlink/reparse point, personal path,
   private hostname/address, credential pattern, or non-English personal artifact exists.
8. `git diff --check` is clean.

The source implementation used to prepare this public reference passed 116 API tests and
10 knowledge lifecycle tests before export. The public translation must pass its own test
suite; it must not inherit that result by assertion.

## Runtime smoke gate

On a real workstation, validate:

- API, Open WebUI, knowledge, and SearXNG are healthy.
- Ollama lists the configured model and produces a real answer.
- Workbench loads without browser console errors.
- native Open WebUI chat streams a real answer.
- `agent.personal-agent` streams a real answer and terminates with `[DONE]`.
- missing/wrong bridge tokens return 401; the configured provider returns 200.
- SearXNG returns at least one curated result and logs no unexpected traceback/5xx.
- knowledge diagnostics show no stale, failed, or missing-root project.
- Agent, orchestrator, and Open WebUI SQLite `quick_check` results are `ok`.

## Durable-run recovery gate

1. Start a slow Agent response.
2. Disconnect the browser after at least one persisted output event.
3. Verify the run continues to completion.
4. Reconnect with the same identity and replay from the last event sequence.
5. Verify the final text contains no duplicated prefix.
6. Repeat while restarting the API during generation.
7. Verify a new attempt claims the same run and the old attempt cannot append.

## Memory gate

- Save a preference through Workbench and observe it through a later Personal Agent chat.
- Save a preference through Personal Agent and observe it in Workbench.
- Verify an unrelated native Ollama chat does not receive that memory.
- Verify a project episode cannot appear in another project or unscoped chat.
- Verify `suppress_memory` and temporary/tool tasks create no durable memory.
- Edit and delete a memory using revision checks.
- Permanently delete a test task and verify run/event/link/source-memory cleanup.

## Context gate

- Unit-test rolling summary thresholds and stable summary provenance.
- Verify an oversized individual turn preserves head and tail and emits a visible warning.
- Verify original history remains present after summary creation.
- Verify the Open WebUI Qwen compaction configuration converges to the declared threshold
  and retention values.

An actual beyond-threshold Open WebUI conversation is an optional hardware acceptance test;
configuration convergence is the portable release gate and must not be described as a
completed long-run test unless it was really executed.

## Public-release gate

- Create a fresh Git history; never publish a private repository's history.
- Inspect staged files, Git author data, remote URLs, and complete history.
- Run the privacy scanner against both the filesystem and staged manifest.
- Verify LICENSE, README, SECURITY, and architecture documentation are present.
- Confirm the repository visibility is public only after all previous gates pass.
