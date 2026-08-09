#!/usr/bin/env python3
"""Validate Python and embedded Workbench JavaScript without creating caches."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOTS = (
    ROOT / "scripts",
    ROOT / "services" / "api" / "app",
    ROOT / "services" / "api" / "tests",
    ROOT / "services" / "knowledge",
)
CONSOLE = ROOT / "services" / "api" / "app" / "static" / "console.html"


def validate_python() -> int:
    count = 0
    for source_root in PYTHON_ROOTS:
        for path in sorted(source_root.rglob("*.py")):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            count += 1
    return count


def console_javascript() -> str:
    html = CONSOLE.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.DOTALL)
    if len(scripts) != 1:
        raise RuntimeError(
            f"Expected one inline Workbench script, found {len(scripts)}"
        )
    return scripts[0]


def validate_javascript(source: str) -> str:
    node = shutil.which("node")
    if node:
        command = [node, "--check"]
        implementation = "host Node.js"
    elif shutil.which("docker"):
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "node:22-alpine",
            "node",
            "--check",
        ]
        implementation = "node:22-alpine"
    else:
        raise RuntimeError("Node.js or Docker is required for JavaScript syntax checks")
    result = subprocess.run(command, input=source.encode("utf-8"), check=False)
    if result.returncode != 0:
        raise RuntimeError("Workbench JavaScript syntax validation failed")
    return implementation


def main() -> int:
    python_count = validate_python()
    javascript_runtime = validate_javascript(console_javascript())
    print(
        f"Source syntax passed: {python_count} Python files; "
        f"Workbench JavaScript via {javascript_runtime}."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, SyntaxError, RuntimeError) as exc:
        print(f"Source syntax failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
