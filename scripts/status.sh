#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:-$project_root/.env}"
python3 "$project_root/scripts/status.py" --env-file "$env_file"
