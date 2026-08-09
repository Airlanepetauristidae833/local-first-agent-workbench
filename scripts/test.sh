#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$project_root/.env}"
[[ -f "$env_file" ]] || {
  echo "Missing environment file. Run bootstrap first." >&2
  exit 1
}
cd "$project_root"
docker compose --env-file "$env_file" config --quiet
bash -n scripts/*.sh
python3 scripts/validate_sources.py
docker compose --env-file "$env_file" --profile test run --build --rm api-tests
docker compose --env-file "$env_file" --profile test run --rm api-tests \
  ruff check --target-version py312 --select E4,E7,E9,F,I app tests
docker compose --env-file "$env_file" --profile test run --build --rm knowledge-tests
python3 scripts/privacy_scan.py
if command -v git >/dev/null 2>&1 &&
  git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git diff --check
fi
