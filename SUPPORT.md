# Support

Local-First Agent Workbench is maintained as an open-source reference project, not a
hosted service. Community support is provided on a best-effort basis without an uptime,
response-time, compatibility, or resolution SLA.

## Choose the right channel

| Need | Channel |
| --- | --- |
| Reproducible defect | [Bug report](https://github.com/Joviei/local-first-agent-workbench/issues/new?template=bug_report.yml) |
| Setup or configuration help | [Configuration help](https://github.com/Joviei/local-first-agent-workbench/issues/new?template=config_help.yml) |
| Proposed capability or design change | [Feature request](https://github.com/Joviei/local-first-agent-workbench/issues/new?template=feature_request.yml) |
| Vulnerability or sensitive security finding | Follow [SECURITY.md](SECURITY.md); do not open a public issue |
| Conduct concern | Follow the private reporting instructions in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) |

Search existing issues and review [README.md](README.md),
[Operations](docs/OPERATIONS.md), and [Acceptance](docs/ACCEPTANCE.md) first.

## Information that helps

For a public support request, provide only redacted, non-sensitive details:

- host operating system and version;
- Docker Engine/Desktop and Compose versions;
- whether access is loopback or through a private overlay network;
- relevant component and image versions;
- Ollama version and model name, without private model paths;
- the exact command that failed and the sanitized error;
- expected and observed behavior; and
- the smallest reproducible sequence.

Never attach `.env`, secrets, tokens, cookies, certificates, database files, personal
documents, real Obsidian vault contents, Tailnet names, private addresses, absolute user
paths, or unredacted logs. Replace them with clear placeholders such as `<TOKEN>`,
`<PRIVATE_HOST>`, and `<LOCAL_PATH>`.

## Supported project boundary

Security fixes target the current `main` branch. General support is concentrated on the
documented single-user, single-node Compose deployment and the versions pinned in the
repository. Maintainers may still consider reports from other environments, but cannot
promise support for:

- public-internet exposure without a separate authenticated security boundary;
- untrusted tenants or multiple API replicas;
- arbitrary reverse proxies, VPNs, model forks, or downstream modifications;
- hardware-specific model performance guarantees; or
- third-party service availability.

Questions outside that boundary are most useful when they include a reusable documentation
improvement or a narrowly scoped patch.

## Security and urgent incidents

This repository does not operate your workstation and cannot provide emergency incident
response. If credentials or private data were exposed, revoke or rotate them immediately,
isolate the affected service as appropriate, and then report a project vulnerability
privately under [SECURITY.md](SECURITY.md).
