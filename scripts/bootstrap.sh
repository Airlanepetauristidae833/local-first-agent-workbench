#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$project_root/.env"
state_root=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      env_file="$2"
      shift 2
      ;;
    --state-root)
      state_root="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

arguments=("$project_root/scripts/bootstrap.py" --env-file "$env_file")
if [[ -n "$state_root" ]]; then
  arguments+=(--state-root "$state_root")
fi
python3 "${arguments[@]}"
echo "Bootstrap complete. Review the environment file and install its configured Ollama model."
