#!/usr/bin/env python3
"""Create local state, secrets, and rendered configuration without publishing it."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ENV = ROOT / ".env.example"
SEARCH_TEMPLATE = ROOT / "config" / "searxng" / "settings.template.yml"
SEARCH_PLACEHOLDER = "__SEARXNG_SECRET_KEY__"
SECRET_RE = re.compile(r"^[0-9a-f]{64}$")


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def update_env(path: Path, updates: dict[str, str]) -> None:
    source = path.read_text(encoding="utf-8").splitlines()
    pending = dict(updates)
    output: list[str] = []
    for line in source:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.split("=", 1)[0].strip()
            if name in pending:
                output.append(f"{name}={pending.pop(name)}")
                continue
        output.append(line)
    if pending:
        output.append("")
        output.extend(f"{name}={value}" for name, value in pending.items())
    atomic_write(path, "\n".join(output).rstrip() + "\n")


def atomic_write(path: Path, content: str, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if private and os.name != "nt":
            temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
        if private and os.name != "nt":
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    finally:
        temporary.unlink(missing_ok=True)


def env_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def compose_path(path: Path) -> str:
    return path.resolve().as_posix()


def ensure_secret(path: Path, name: str) -> str:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Secret path is not a regular file: {path}")
        values = parse_env(path)
        value = values.get(name, "")
        if not SECRET_RE.fullmatch(value):
            raise RuntimeError(f"Secret file is malformed: {path}")
        return value
    value = secrets.token_hex(32)
    atomic_write(path, f"{name}={value}\n", private=True)
    return value


def ensure_directories(paths: list[Path]) -> None:
    for path in paths:
        if path.exists() and path.is_symlink():
            raise RuntimeError(f"Refusing a symlinked state directory: {path}")
        path.mkdir(parents=True, exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--state-root", type=Path)
    args = parser.parse_args()

    env_file = args.env_file.expanduser().resolve()
    if not env_file.exists():
        env_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(env_file, EXAMPLE_ENV.read_text(encoding="utf-8"))
    if env_file.is_symlink() or not env_file.is_file():
        raise RuntimeError(f"Environment path is not a regular file: {env_file}")

    values = parse_env(env_file)
    if args.state_root is not None:
        state_root = args.state_root.expanduser().resolve()
        workspace_root = state_root / "workspaces"
        vault_root = state_root / "vault"
        source_root = state_root / "knowledge-sources"
        update_env(
            env_file,
            {
                "LOCAL_STATE_ROOT": compose_path(state_root),
                "WORKSPACE_HOST_ROOT": compose_path(workspace_root),
                "OBSIDIAN_VAULT_HOST_ROOT": compose_path(vault_root),
                "KNOWLEDGE_SOURCE_HOST_ROOT": compose_path(source_root),
            },
        )
        values = parse_env(env_file)
    else:
        state_root = env_path(values.get("LOCAL_STATE_ROOT", "./runtime"))
        workspace_root = env_path(values.get("WORKSPACE_HOST_ROOT", "./workspaces"))
        vault_root = env_path(values.get("OBSIDIAN_VAULT_HOST_ROOT", "./vault"))
        source_root = env_path(
            values.get("KNOWLEDGE_SOURCE_HOST_ROOT", "./knowledge-sources")
        )

    if state_root == ROOT:
        raise RuntimeError("LOCAL_STATE_ROOT must not be the repository root")
    directories = [
        state_root / "data" / "api",
        state_root / "data" / "knowledge",
        state_root / "data" / "open-webui",
        state_root / "logs" / "api",
        state_root / "config" / "searxng",
        state_root / "secrets",
        workspace_root,
        vault_root,
        source_root,
    ]
    ensure_directories(directories)

    secrets_root = state_root / "secrets"
    ensure_secret(secrets_root / "open-webui.env", "WEBUI_SECRET_KEY")
    ensure_secret(
        secrets_root / "agent-bridge.env", "PERSONAL_AGENT_BRIDGE_TOKEN"
    )
    search_secret = ensure_secret(
        secrets_root / "searxng.env", "SEARXNG_SECRET_KEY"
    )

    template = SEARCH_TEMPLATE.read_text(encoding="utf-8")
    if template.count(SEARCH_PLACEHOLDER) != 1:
        raise RuntimeError("SearXNG template must contain exactly one secret placeholder")
    rendered = template.replace(SEARCH_PLACEHOLDER, search_secret)
    atomic_write(
        state_root / "config" / "searxng" / "settings.yml",
        rendered,
        private=True,
    )

    vault_template = ROOT / "templates" / "obsidian-vault"
    if vault_template.is_dir() and not (vault_root / "Home.md").exists():
        shutil.copytree(vault_template, vault_root, dirs_exist_ok=True)

    print(
        json.dumps(
            {
                "status": "ready",
                "env_file": str(env_file),
                "state_root": str(state_root),
                "secrets_created_or_validated": 3,
                "search_config_rendered": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
