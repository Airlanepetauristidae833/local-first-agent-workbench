#!/usr/bin/env python3
"""Validate Python and embedded Workbench JavaScript without creating caches."""

from __future__ import annotations

import ast
from html.parser import HTMLParser
from pathlib import Path
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


class _ScriptCollector(HTMLParser):
    """Collect script elements using the same case-insensitive tag rules as HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[tuple[bool, list[str]]] = []
        self._current_script: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "script":
            return
        content: list[str] = []
        self.scripts.append((any(name == "src" for name, _ in attrs), content))
        self._current_script = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._current_script = None

    def handle_data(self, data: str) -> None:
        if self._current_script is not None:
            self._current_script.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._current_script is not None:
            self._current_script.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if self._current_script is not None:
            self._current_script.append(f"&#{name};")

    @property
    def has_unclosed_script(self) -> bool:
        return self._current_script is not None


def validate_python() -> int:
    count = 0
    for source_root in PYTHON_ROOTS:
        for path in sorted(source_root.rglob("*.py")):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            count += 1
    return count


def _inline_javascript(html: str) -> str:
    parser = _ScriptCollector()
    parser.feed(html)
    parser.close()
    if parser.has_unclosed_script:
        raise RuntimeError("Workbench contains an unclosed script element")
    if len(parser.scripts) != 1:
        raise RuntimeError(
            f"Expected one inline Workbench script, found {len(parser.scripts)}"
        )
    has_src, content = parser.scripts[0]
    if has_src:
        raise RuntimeError("Expected the Workbench script to be inline, found src")
    return "".join(content)


def console_javascript() -> str:
    return _inline_javascript(CONSOLE.read_text(encoding="utf-8"))


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
