# Maintainers

## Current maintainer

- [@Joviei](https://github.com/Joviei) — project owner and primary maintainer.

The primary maintainer is responsible for project direction, issue triage, pull-request review, release management, dependency updates, and security coordination.

## Maintenance workflow

The repository uses public, reproducible automation for routine maintenance:

- `release-validation` runs source validation, API and knowledge tests, linting, privacy checks, and Docker Compose cleanup on every branch push and pull request.
- `CodeQL` analyzes Python and JavaScript/TypeScript on changes to `main`, pull requests, and a weekly schedule.
- `Dependency review` blocks pull requests that introduce dependencies with known high- or critical-severity vulnerabilities.
- Dependabot checks Python, GitHub Actions, and container dependencies each week.
- A semantic-version tag runs the complete validation workflow before publishing a GitHub release with a deterministic source archive, SHA-256 checksum, and SPDX SBOM.

## Contribution and review

Changes should be proposed through pull requests. The maintainer reviews correctness, backward compatibility, privacy boundaries, test coverage, and documentation before merging. Automated checks are required evidence, but they do not replace human review.

Maintenance is provided on a best-effort basis. Issues that include reproduction steps, relevant logs with secrets removed, and environment details are easier to prioritize and resolve.

## Supported releases

Security and compatibility fixes target the latest published release. Users should reproduce a problem on the latest release before reporting it whenever possible.

For vulnerability reports, do not open a public issue. Follow the private reporting process in [SECURITY.md](../SECURITY.md).

## Release policy

Releases use semantic version tags such as `v1.0.0`:

- patch releases contain backward-compatible fixes;
- minor releases add backward-compatible functionality;
- major releases may contain breaking changes.

Release notes should describe user-visible changes, migration requirements, known limitations, and verification results.
