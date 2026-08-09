#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$project_root/.env}"
[[ -f "$env_file" ]] || { echo "Missing environment file." >&2; exit 1; }
cd "$project_root"
docker compose --env-file "$env_file" down
