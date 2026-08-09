#!/usr/bin/env python3
"""Report container and loopback endpoint health without exposing configuration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SERVICES = {"api", "knowledge", "search", "open-webui"}


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def compose_rows(env_file: Path) -> list[dict]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "ps",
            "--format",
            "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("docker compose ps failed")
    output = result.stdout.strip()
    if not output:
        return []
    try:
        parsed = json.loads(output)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        return [json.loads(line) for line in output.splitlines() if line.strip()]


def endpoint(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "health-check"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return {"url": url, "status": response.status, "ok": response.status == 200}
    except (urllib.error.URLError, TimeoutError):
        return {"url": url, "status": None, "ok": False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    env_file = args.env_file.expanduser().resolve()
    if not env_file.is_file():
        raise RuntimeError("Environment file is missing; run bootstrap first")
    values = parse_env(env_file)
    rows = compose_rows(env_file)
    containers: dict[str, dict] = {}
    for row in rows:
        service = str(row.get("Service") or row.get("service") or "")
        if service not in REQUIRED_SERVICES:
            continue
        state = str(row.get("State") or row.get("state") or "").lower()
        health = str(row.get("Health") or row.get("health") or "").lower()
        containers[service] = {
            "state": state,
            "health": health,
            "ok": state == "running" and health == "healthy",
        }
    for missing in REQUIRED_SERVICES - containers.keys():
        containers[missing] = {"state": "missing", "health": "", "ok": False}

    api_port = int(values.get("API_PORT", "8000"))
    webui_port = int(values.get("OPEN_WEBUI_PORT", "3000"))
    endpoints = [
        endpoint(f"http://127.0.0.1:{api_port}/health"),
        endpoint(f"http://127.0.0.1:{webui_port}/api/version"),
    ]
    ok = all(item["ok"] for item in containers.values()) and all(
        item["ok"] for item in endpoints
    )
    print(
        json.dumps(
            {
                "status": "healthy" if ok else "degraded",
                "containers": containers,
                "endpoints": endpoints,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
