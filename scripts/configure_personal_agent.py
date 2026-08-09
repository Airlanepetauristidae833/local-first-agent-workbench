#!/usr/bin/env python3
"""Idempotently register the durable Personal Agent provider in Open WebUI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
SECRET_RE = re.compile(r"^[0-9a-f]{64}$")
PROVIDER_URL = "http://api:8000/v1"
PROVIDER_MODEL = "personal-agent"


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_from_root(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def request_json(
    method: str,
    url: str,
    *,
    bearer: str,
    payload: dict | None = None,
    timeout: int = 60,
) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach {url}: {exc.reason}") from exc


def bridge_token(state_root: Path) -> str:
    path = state_root / "secrets" / "agent-bridge.env"
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("The bridge secret is missing; run bootstrap first")
    token = parse_env(path).get("PERSONAL_AGENT_BRIDGE_TOKEN", "")
    if not SECRET_RE.fullmatch(token):
        raise RuntimeError("The bridge secret is malformed; rerun bootstrap")
    return token


def admin_token(env_file: Path) -> str | None:
    source = """
import sqlite3
from datetime import timedelta
from open_webui.utils.auth import create_token

connection = sqlite3.connect(
    'file:/app/backend/data/webui.db?mode=ro', uri=True
)
row = connection.execute(
    "select id from user where role='admin' order by created_at limit 1"
).fetchone()
if row is None:
    print('__NO_ADMIN__')
else:
    print(create_token({'id': row[0]}, timedelta(minutes=10)))
""".strip()
    command = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "exec",
        "-T",
        "open-webui",
        "python",
        "-c",
        source,
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Open WebUI could not issue a temporary admin token")
    output = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not output or output[-1] == "__NO_ADMIN__":
        return None
    return output[-1]


def normalized_url(value: str) -> str:
    return value.strip().rstrip("/")


def configure_provider(
    webui_url: str,
    admin: str,
    token: str,
) -> dict:
    current = request_json(
        "GET", f"{webui_url}/openai/config", bearer=admin, timeout=30
    )
    urls = list(current.get("OPENAI_API_BASE_URLS") or [])
    keys = list(current.get("OPENAI_API_KEYS") or [])
    configs = current.get("OPENAI_API_CONFIGS") or {}
    entries: list[tuple[str, str, dict | None]] = []
    owned_found = False
    pruned_empty_default = False

    for index, raw_url in enumerate(urls):
        url = str(raw_url)
        key = str(keys[index]) if index < len(keys) and keys[index] else ""
        config = configs.get(str(index), configs.get(url))
        if (
            normalized_url(url) == "https://api.openai.com/v1"
            and not key.strip()
            and config is None
        ):
            pruned_empty_default = True
            continue
        if normalized_url(url) == normalized_url(PROVIDER_URL):
            if owned_found:
                continue
            owned_found = True
        entries.append((url, key, config))

    if not owned_found:
        entries.append((PROVIDER_URL, "", None))

    owned_config = {
        "enable": True,
        "prefix_id": "agent",
        "model_ids": [PROVIDER_MODEL],
        "auth_type": "bearer",
        "connection_type": "external",
        "headers": {
            "X-OpenWebUI-Chat-Id": "{{CHAT_ID}}",
            "X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}",
            "X-OpenWebUI-User-Message-Id": "{{USER_MESSAGE_ID}}",
            "X-OpenWebUI-User-Message-Parent-Id": "{{USER_MESSAGE_PARENT_ID}}",
            "X-OpenWebUI-Task": "{{TASK}}",
            "X-OpenWebUI-User-Id": "{{USER_ID}}",
        },
    }
    next_urls: list[str] = []
    next_keys: list[str] = []
    next_configs: dict[str, dict] = {}
    for index, (url, key, config) in enumerate(entries):
        next_urls.append(url)
        if normalized_url(url) == normalized_url(PROVIDER_URL):
            next_keys.append(token)
            next_configs[str(index)] = owned_config
        else:
            next_keys.append(key)
            if config is not None:
                next_configs[str(index)] = config

    updated = request_json(
        "POST",
        f"{webui_url}/openai/config/update",
        bearer=admin,
        payload={
            "ENABLE_OPENAI_API": True,
            "OPENAI_API_BASE_URLS": next_urls,
            "OPENAI_API_KEYS": next_keys,
            "OPENAI_API_CONFIGS": next_configs,
        },
        timeout=30,
    )
    updated_urls = list(updated.get("OPENAI_API_BASE_URLS") or [])
    owned_count = sum(
        normalized_url(str(url)) == normalized_url(PROVIDER_URL)
        for url in updated_urls
    )
    if owned_count != 1:
        raise RuntimeError("Personal Agent provider registration did not converge")
    models = request_json(
        "GET", f"{webui_url}/openai/models", bearer=admin, timeout=60
    )
    model_ids = {str(item.get("id")) for item in models.get("data", [])}
    if "agent.personal-agent" not in model_ids:
        raise RuntimeError("Open WebUI did not expose agent.personal-agent")
    return {
        "provider_count": len(updated_urls),
        "personal_agent_provider_count": owned_count,
        "pruned_empty_default_provider": pruned_empty_default,
    }


def configure_compaction(
    webui_url: str,
    admin: str,
    model: str,
    threshold: int,
    retention: int,
) -> None:
    all_models = request_json(
        "GET", f"{webui_url}/api/models", bearer=admin, timeout=60
    )
    model_ids = {str(item.get("id")) for item in all_models.get("data", [])}
    if model not in model_ids:
        raise RuntimeError(f"Context compaction model is unavailable: {model}")
    current = request_json(
        "GET", f"{webui_url}/api/v1/chats/config", bearer=admin, timeout=30
    )
    updated = request_json(
        "POST",
        f"{webui_url}/api/v1/chats/config",
        bearer=admin,
        payload={
            "CONTEXT_COMPACTION_MODEL": model,
            "ENABLE_CONTEXT_COMPACTION": True,
            "CONTEXT_COMPACTION_TOKEN_THRESHOLD": threshold,
            "CONTEXT_COMPACTION_TOKEN_CAP": threshold,
            "CONTEXT_COMPACTION_RETENTION_PERCENTAGE": retention,
            "CONTEXT_COMPACTION_PROMPT_TEMPLATE": str(
                current.get("CONTEXT_COMPACTION_PROMPT_TEMPLATE") or ""
            ),
        },
        timeout=30,
    )
    expected = (
        bool(updated.get("ENABLE_CONTEXT_COMPACTION"))
        and updated.get("CONTEXT_COMPACTION_MODEL") == model
        and int(updated.get("CONTEXT_COMPACTION_TOKEN_THRESHOLD", 0)) == threshold
        and int(updated.get("CONTEXT_COMPACTION_TOKEN_CAP", 0)) == threshold
        and int(updated.get("CONTEXT_COMPACTION_RETENTION_PERCENTAGE", 0))
        == retention
    )
    if not expected:
        raise RuntimeError("Open WebUI context compaction did not converge")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--allow-pending-admin", action="store_true")
    args = parser.parse_args()
    env_file = args.env_file.expanduser().resolve()
    if not env_file.is_file():
        raise RuntimeError("Environment file is missing; run bootstrap first")
    values = parse_env(env_file)
    state_root = resolve_from_root(values.get("LOCAL_STATE_ROOT", "./runtime"))
    token = bridge_token(state_root)
    api_port = int(values.get("API_PORT", "8000"))
    webui_port = int(values.get("OPEN_WEBUI_PORT", "3000"))
    agent_url = f"http://127.0.0.1:{api_port}"
    webui_url = f"http://127.0.0.1:{webui_port}"

    gateway_models = request_json(
        "GET", f"{agent_url}/v1/models", bearer=token, timeout=30
    )
    if PROVIDER_MODEL not in {
        str(item.get("id")) for item in gateway_models.get("data", [])
    }:
        raise RuntimeError("Personal Agent gateway model discovery failed")

    admin = admin_token(env_file)
    if admin is None:
        if not args.allow_pending_admin:
            raise RuntimeError(
                "Create the first Open WebUI administrator, then rerun start"
            )
        print(
            json.dumps(
                {
                    "status": "pending_admin",
                    "next_step": "Create the first Open WebUI administrator and rerun start.",
                },
                sort_keys=True,
            )
        )
        return 0

    threshold = int(
        values.get("OPENWEBUI_CONTEXT_COMPACTION_TOKEN_THRESHOLD", "10000")
    )
    retention = int(
        values.get("OPENWEBUI_CONTEXT_COMPACTION_RETENTION_PERCENTAGE", "40")
    )
    model = values.get(
        "OPENWEBUI_CONTEXT_COMPACTION_MODEL",
        values.get("OLLAMA_MODEL", ""),
    ).strip()
    if threshold < 1 or not 10 <= retention <= 50 or not model:
        raise RuntimeError("Context compaction settings are invalid")

    result = configure_provider(webui_url, admin, token)
    configure_compaction(webui_url, admin, model, threshold, retention)
    print(
        json.dumps(
            {
                "status": "configured",
                **result,
                "personal_agent_model": "agent.personal-agent",
                "context_compaction_model": model,
                "context_compaction_token_threshold": threshold,
                "context_compaction_retention_percentage": retention,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
