# Operations

## Configuration

Copy `.env.example` to `.env` and review at least:

- host bind/port values
- Ollama base URL and installed model name
- host path for the read-only workspace registry
- context and watchdog budgets
- whether you intend to run the optional, separately managed Codex worker

Keep human-facing ports on loopback unless an authenticated private reverse proxy owns the
external boundary.

## Bootstrap

Bootstrap creates ignored runtime directories, generates independent secrets, renders the
SearXNG template, and verifies that no placeholder remains.

```powershell
.\scripts\bootstrap.ps1
```

```bash
./scripts/bootstrap.sh
```

Generated files must remain untracked.

## Start, status, and stop

```powershell
.\scripts\start.ps1
.\scripts\status.ps1
.\scripts\stop.ps1
```

```bash
./scripts/start.sh
./scripts/status.sh
./scripts/stop.sh
```

Start waits for the API, knowledge, search, and Open WebUI containers. It then reconciles a
single Personal Agent provider without deleting unrelated configured providers.

The first start can only report `pending_admin`, because Open WebUI has no administrator
yet. Create the first administrator and rerun the start script; provider and context-
compaction configuration is idempotent.

## Optional Codex handoff worker

The included worker is a Windows PowerShell reference and is intentionally not started by
Compose. Authenticate the Codex app/CLI first, start the stack, approve a Codex-routed
stage in Workbench, and then run:

```powershell
.\scripts\codex-worker.ps1
```

Run it under the same trusted Windows account that owns the selected implementation
workspace. The worker claims a leased attempt, uses an isolated execution directory,
persists its result before acknowledgement, and stops stale attempts after lease loss.
Do not run multiple copies against a shared writable workspace.

## Open WebUI first-run checklist

1. Create the first administrator account.
2. Disable public sign-up.
3. Confirm the native Ollama model appears.
4. Confirm `agent.personal-agent` appears.
5. Send a direct native-model test.
6. Send a Personal Agent test and verify it appears in durable run history.

## Monitoring

Status should verify:

- four healthy containers and zero unexpected restart/OOM state
- Ollama readiness and configured model availability
- Agent and knowledge database integrity
- knowledge index diagnostics
- a bounded SearXNG functional search
- bridge provider count and authentication
- local endpoints, plus optional private remote endpoints

Third-party search failures are external degradation. The workflow must fall back explicitly
and report unverified output rather than invent citations.

## Backup and restore

Back up, at minimum:

- `data/api`
- `data/knowledge`
- `data/open-webui`
- the human-maintained Obsidian vault
- optional execution workspaces and handoff results

Exclude secrets and authentication state from ordinary portable backups; back them up only
through a separate encrypted secret-management procedure. Stop writers or use database-safe
snapshot methods before copying SQLite files.

Restore into an empty runtime directory, validate SQLite `quick_check`, then start and let
the knowledge service verify/rebuild derived indexes. Never use `docker compose down -v`
unless data loss is intentional.

## Upgrade procedure

1. Read release notes and pin image versions.
2. Create a consistent backup.
3. Pull/build images.
4. Run API and knowledge tests.
5. Start the stack and wait for health.
6. Reconcile the Personal Agent provider.
7. Run direct Qwen, Personal Agent, knowledge, and reconnect smoke tests.
8. Roll back the code/image pin and restore the pre-upgrade database only if schema
   compatibility requires it.

## Troubleshooting long responses

- A visible thinking timer is not proof that the browser is receiving answer tokens.
- Check the durable run status and event sequence before retrying.
- Reconnect with the same idempotency identity; do not create a second run.
- Inspect Ollama first-token/idle watchdog events.
- Confirm only the intended model is loaded when GPU memory is constrained.
- Keep model output expectations separate from network transport health.
