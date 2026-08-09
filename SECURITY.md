# Security Policy

## Supported version

Security fixes target the current `main` branch. This is a self-hosted reference project,
not a managed multi-tenant service.

## Report a vulnerability

Please use the repository's private GitHub Security Advisory flow. Do not open a public
issue containing credentials, private hostnames, personal documents, database samples, or
reproduction data that belongs to another person.

## Deployment boundary

The default deployment assumes one trusted owner and one trusted host:

- API and Open WebUI bind to loopback.
- Knowledge and SearXNG are reachable only on the Compose network.
- Private remote access should use an authenticated overlay network such as Tailscale.
- Do not expose Ollama, FastAPI, Open WebUI, SQLite files, or SearXNG directly to the public
  internet. Do not enable Tailscale Funnel for this stack.

Changing a bind address to `0.0.0.0` materially changes the threat model. Add an
authenticated reverse proxy, TLS, rate limits, CSRF/origin controls, and tenant-aware
authorization before doing so.

## Secrets

Bootstrap generates separate random values for:

- Open WebUI session signing
- the Open WebUI-to-Agent Bearer bridge
- SearXNG server configuration

Secrets belong in the ignored `secrets/` directory. Never place them in Compose YAML,
`.env.example`, source files, issue reports, screenshots, test fixtures, or diagnostics.
Rotate a secret if it appears in a terminal transcript or Git history.

## Data at rest

SQLite databases, Obsidian Markdown, uploaded documents, and vector indexes are plaintext
on disk. Filesystem permissions reduce accidental local access but do not replace full-disk
encryption. Use BitLocker, FileVault, or an equivalent control when offline device loss is
in scope.

Backups must receive the same protection as the live data. Validate backup targets and
never bundle secrets, authentication state, or unrelated knowledge vaults by default.

## Agent and prompt-injection boundary

- RAG documents, attachments, web pages, quoted text, and model output are untrusted data.
- They cannot grant file, network, Codex, or shell authority.
- The generic workspace registry is read-only.
- Optional Codex execution is isolated to a dedicated execution workspace and should occur
  only after the user has approved the project route.
- Temporary Open WebUI tasks and requests marked `suppress_memory` must not create durable
  user memory.

## Memory privacy

Global memory is limited to stable preferences, constraints, and facts. Project episodes
and experience remain project-scoped. Users can inspect, revise, delete, or suppress memory
creation. External chat identifiers are source-scoped hashes; display names and email
addresses are not required by the bridge.

## Release hygiene

Run the fail-closed privacy scanner before every push. A release must reject:

- `.env`, secrets, tokens, keys, certificates, databases, logs, backups, archives, caches,
  model files, and actual vault content
- symlinks or Windows reparse points
- private/Tailnet hostnames, private addresses, usernames, email addresses, absolute local
  paths, and non-English personal artifacts
- files outside the explicit public manifest

If the scanner itself errors or observes zero files, the release fails.
