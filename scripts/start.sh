#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$project_root/.env"
skip_provider=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      env_file="$2"
      shift 2
      ;;
    --skip-provider-config)
      skip_provider=true
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

[[ -f "$env_file" ]] || {
  echo "Missing environment file. Run scripts/bootstrap.sh first." >&2
  exit 1
}
startup_timeout="$(awk -F= '/^STARTUP_TIMEOUT_SECONDS=/{print $2}' "$env_file" | tail -1)"
startup_timeout="${startup_timeout:-360}"

cd "$project_root"
docker compose --env-file "$env_file" up -d --build --wait \
  --wait-timeout "$startup_timeout" api knowledge search open-webui
if [[ "$skip_provider" != true ]]; then
  python3 scripts/configure_personal_agent.py \
    --env-file "$env_file" --allow-pending-admin
fi
python3 scripts/status.py --env-file "$env_file"
