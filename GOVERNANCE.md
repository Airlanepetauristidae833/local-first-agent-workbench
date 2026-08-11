# Governance

Local-First Agent Workbench uses lightweight, maintainer-led governance. The purpose is to
keep decisions understandable and reviewable while the contributor community is small.

## Principles

- Preserve local-first operation, user control, privacy, and explicit authority boundaries.
- Prefer small, reversible decisions supported by tests and operational evidence.
- Record material design decisions in public issues or pull requests unless security or
  privacy requires a private channel.
- Keep the documented scope honest; do not imply service levels or deployment guarantees
  the project does not provide.

## Roles

### Users

Users operate the project, report defects, request capabilities, and share reproducible
feedback. No contribution history is required to participate.

### Contributors

Contributors submit documentation, tests, designs, code, or review feedback under the
repository license and community standards.

### Maintainers

Maintainers triage issues, review and merge changes, protect the release and security
boundaries, and manage versions. They may close work that is unsafe, out of scope,
unmaintainable, or insufficiently reproducible, with an explanation when disclosure is
safe.

### Project lead

The repository owner acts as project lead and has final responsibility for scope,
maintainer appointments, releases, security response, and unresolved technical decisions.
This role is a decision backstop, not a guarantee of support or delivery.

## Decisions

Routine fixes and documentation changes are decided through pull-request review. Material
changes to persistence, compatibility, network exposure, deletion, memory, knowledge
provenance, or tool authority should begin with a design issue that states:

1. the user problem and evidence;
2. alternatives considered;
3. data, migration, recovery, privacy, and security effects; and
4. a validation and rollback plan.

Maintainers seek rough consensus, meaning major concerns have been understood and
addressed even if every participant does not prefer the outcome. When consensus is not
practical, the project lead makes and records the decision. Security-sensitive details are
handled privately and disclosed later only when safe.

## Changes and releases

- Every merge requires maintainer review and a successful required validation gate.
- Passing automation does not replace architectural, privacy, or operational review.
- Breaking configuration or persisted-state changes require clear migration and rollback
  notes before release.
- Versioned releases should use semantic versioning where the impact can be expressed that
  way, with release notes describing user-visible changes and known limitations.
- Maintainers may make an urgent security fix privately, then publish the minimum safe
  advisory and release information.

## Becoming or leaving a maintainer

Existing maintainers may invite a contributor who has demonstrated sustained, constructive
work, sound judgment across project boundaries, dependable review, and respect for user
privacy. There is no contribution-count threshold.

A maintainer may step down at any time. Access may also be removed for prolonged
unavailability, repeated disregard of project responsibilities, or a code-of-conduct or
security breach. When possible, maintainers should transfer open reviews and release
context before leaving.

## Amendments

Governance changes use the same public issue and pull-request process as other material
changes. The project lead may apply an immediate temporary rule to address an active
security or community-safety risk, followed by a documented review when it is safe to do
so.
