## Summary

<!-- Explain the user-visible problem and the solution. Keep the change focused. -->

## Related issue

<!-- Use "Closes #123" when applicable, or explain why no issue is needed. -->

## Change type

- [ ] Bug fix
- [ ] Feature
- [ ] Documentation
- [ ] Refactor or maintenance
- [ ] Security hardening
- [ ] Breaking configuration or persisted-state change

## Design and boundary impact

<!--
Describe effects on durable runs, persistence/migration, memory, knowledge provenance,
network exposure, privacy, deletion/recovery, and local-model/Codex authority. Write "No
material impact" only after considering these boundaries.
-->

## Validation

<!-- List exact commands and results. Distinguish automated checks from real hardware, model, browser, and remote-access checks. -->

- [ ] `./scripts/test.sh` or `.\scripts\test.ps1`
- [ ] Relevant runtime smoke checks
- [ ] Migration and rollback tested, if applicable

Details or limitations:

## Privacy and security review

- [ ] No secrets, credentials, private network identifiers, personal data, real knowledge
      content, databases, logs, archives, or absolute personal paths are included.
- [ ] New public files are intentional and listed in `public-manifest.txt`.
- [ ] Untrusted knowledge, web content, attachments, and model output do not gain tool
      authority.
- [ ] Host ports remain loopback-only by default, or the changed threat model is documented
      and reviewed.
- [ ] Destructive behavior, persisted-state changes, and recovery paths are documented and
      tested where applicable.

## Contributor checklist

- [ ] The change is narrowly scoped and contains no unrelated generated files or formatting.
- [ ] Tests cover new or changed observable behavior.
- [ ] User-facing configuration and operational changes are documented.
- [ ] Backward incompatibilities and known limitations are explicit.
- [ ] I have read `CONTRIBUTING.md`, `SECURITY.md`, and the Code of Conduct.
