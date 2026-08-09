#!/usr/bin/env python3
"""Fail closed when a release tree or Git index contains private artifacts."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "public-manifest.txt"
MAX_FILE_BYTES = 2_000_000
FORBIDDEN_DIRECTORIES = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "backups",
    "cache",
    "caches",
    "client-config",
    "data",
    "execution-workspaces",
    "knowledge-sources",
    "logs",
    "node_modules",
    "runtime",
    "secrets",
    "vault",
    "venv",
    "workspaces",
}
FORBIDDEN_FILENAMES = {
    ".env",
    ".ds_store",
    "thumbs.db",
}
FORBIDDEN_SUFFIXES = {
    ".bak",
    ".cer",
    ".crt",
    ".db",
    ".gz",
    ".jsonl",
    ".key",
    ".log",
    ".ovpn",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".sqlite",
    ".sqlite3",
    ".swp",
    ".tar",
    ".tmp",
    ".zip",
}
PATTERNS = {
    "CJK text": re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"),
    "RFC1918 IPv4 address": re.compile(
        r"(?<!\d)(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
        r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?!\d)"
    ),
    "private IPv6 address": re.compile(
        r"(?<![0-9a-f])(?:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f]):[0-9a-f:]+",
        re.I,
    ),
    "Windows absolute path": re.compile(
        r"(?m)(?:^|[\s'\"=(])(?:[A-Za-z]:[\\/])"
    ),
    "personal home path": re.compile(r"/(?:Users|home)/[^/\s'\"$<>]+", re.I),
    "Tailnet hostname": re.compile(r"\b[a-z0-9-]+\.[a-z0-9-]+\.ts\.net\b", re.I),
    "private key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    ),
    "GitHub token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
    ),
    "OpenAI or Anthropic key": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,})\b"
    ),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    "Stripe live key": re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}\b"),
    "PyPI token": re.compile(r"\bpypi-[0-9A-Za-z_-]{30,}\b"),
    "JWT": re.compile(
        r"\beyJ[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}\."
        r"[0-9A-Za-z_-]{10,}\b"
    ),
    "literal UUID": re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        re.I,
    ),
    "hard-coded credential": re.compile(
        r"(?i)(?:secret(?:_key)?|api[_-]?key|access[_-]?token|"
        r"auth[_-]?token|password)\s*[:=]\s*['\"]?"
        r"(?!generate-me\b|change-me\b|example\b|test-only\b)"
        r"[A-Za-z0-9_+./=-]{24,}['\"]?"
    ),
}
EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b"
)
SAFE_EMAIL_DOMAINS = {"example.com", "example.net", "example.org", "example.invalid"}


def is_reparse_stat(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def is_reparse_path(path: Path) -> bool:
    try:
        return is_reparse_stat(path.lstat())
    except OSError:
        return False


def manifest_entries(path: Path) -> tuple[set[PurePosixPath], list[str]]:
    failures: list[str] = []
    if not path.is_file() or path.is_symlink():
        return set(), ["manifest is missing or is not a regular file"]
    entries: set[PurePosixPath] = set()
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "\\" in line or any(character in line for character in "*?[]"):
            failures.append(f"manifest line {line_number} is not an exact path")
            continue
        entry = PurePosixPath(line)
        if entry.is_absolute() or ".." in entry.parts or str(entry) in {"", "."}:
            failures.append(f"manifest line {line_number} is unsafe")
            continue
        if entry in entries:
            failures.append(f"manifest contains duplicate path: {entry}")
        entries.add(entry)
    manifest_relative = PurePosixPath(path.relative_to(ROOT).as_posix())
    if manifest_relative not in entries:
        failures.append("manifest must list itself")
    return entries, failures


def walk_release_tree() -> tuple[dict[PurePosixPath, Path], list[str]]:
    files: dict[PurePosixPath, Path] = {}
    failures: list[str] = []
    if ROOT.is_symlink() or is_reparse_path(ROOT):
        failures.append("release root must not be a symlink or reparse point")
        return files, failures

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            failures.append(f"cannot enumerate directory: {directory}: {exc}")
            return
        for entry in entries:
            path = Path(entry.path)
            relative = PurePosixPath(path.relative_to(ROOT).as_posix())
            try:
                reparse_point = is_reparse_stat(entry.stat(follow_symlinks=False))
            except OSError as exc:
                failures.append(f"cannot inspect filesystem entry: {relative}: {exc}")
                continue
            if relative.parts == (".git",):
                if (
                    entry.is_symlink()
                    or reparse_point
                    or not entry.is_dir(follow_symlinks=False)
                ):
                    failures.append(".git must be a real directory when present")
                continue
            if entry.is_symlink() or reparse_point:
                failures.append(f"symlink or reparse point is forbidden: {relative}")
                continue
            if entry.is_dir(follow_symlinks=False):
                if entry.name.casefold() in FORBIDDEN_DIRECTORIES:
                    failures.append(f"forbidden directory: {relative}")
                    continue
                visit(path)
            elif entry.is_file(follow_symlinks=False):
                files[relative] = path
            else:
                failures.append(f"unsupported filesystem entry: {relative}")

    visit(ROOT)
    return files, failures


def scan_bytes(relative: PurePosixPath, content: bytes) -> list[str]:
    failures: list[str] = []
    name = relative.name.casefold()
    suffix = PurePosixPath(name).suffix
    if name in FORBIDDEN_FILENAMES or (
        name.startswith(".env.") and name != ".env.example"
    ):
        failures.append(f"forbidden filename: {relative}")
    if suffix in FORBIDDEN_SUFFIXES:
        failures.append(f"forbidden file type: {relative}")
    if len(content) > MAX_FILE_BYTES:
        failures.append(f"file exceeds {MAX_FILE_BYTES} bytes: {relative}")
        return failures
    if b"\x00" in content:
        failures.append(f"binary file is forbidden: {relative}")
        return failures
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        failures.append(f"file is not UTF-8 text: {relative}")
        return failures
    for label, pattern in PATTERNS.items():
        if pattern.search(text):
            failures.append(f"{label}: {relative}")
    for match in EMAIL_RE.finditer(text):
        if match.group(1).casefold() not in SAFE_EMAIL_DOMAINS:
            failures.append(f"non-example email address: {relative}")
            break
    return failures


def run_git(arguments: list[str], *, binary: bool = False) -> bytes | str:
    return subprocess.check_output(
        ["git", *arguments], cwd=ROOT, text=not binary, stderr=subprocess.DEVNULL
    )


def staged_files(require_git: bool) -> tuple[dict[PurePosixPath, bytes], list[str], bool]:
    failures: list[str] = []
    try:
        git_root = Path(str(run_git(["rev-parse", "--show-toplevel"])).strip()).resolve()
    except (FileNotFoundError, subprocess.CalledProcessError):
        if require_git:
            failures.append("Git index scan was required but no repository was found")
        return {}, failures, False
    if git_root != ROOT.resolve():
        failures.append("release root must be the Git repository root")
        return {}, failures, True

    names = str(
        run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    ).splitlines()
    staged: dict[PurePosixPath, bytes] = {}
    for name in names:
        relative = PurePosixPath(name)
        try:
            mode_line = str(run_git(["ls-files", "--stage", "--", name])).strip()
            mode = mode_line.split(maxsplit=1)[0] if mode_line else ""
            if mode == "120000":
                failures.append(f"staged symlink is forbidden: {relative}")
                continue
            staged[relative] = bytes(run_git(["show", f":{name}"], binary=True))
        except subprocess.CalledProcessError:
            failures.append(f"cannot read staged file: {relative}")
    return staged, failures, True


def denylist_values(path: Path | None) -> tuple[list[str], list[str]]:
    if path is None:
        return [], []
    path = path.expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        return [], ["configured private denylist is missing or unsafe"]
    values = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if any(len(value) < 3 for value in values):
        return [], ["private denylist entries must contain at least three characters"]
    return values, []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--denylist", type=Path)
    parser.add_argument("--require-git", action="store_true")
    parser.add_argument(
        "--staged",
        action="store_true",
        help="require Git and scan the full tree plus staged index content",
    )
    args = parser.parse_args()
    manifest = args.manifest.expanduser().resolve()
    if ROOT not in manifest.parents:
        print("Privacy scan failed:\n- manifest must be inside the release root")
        return 1

    expected, failures = manifest_entries(manifest)
    files, tree_failures = walk_release_tree()
    failures.extend(tree_failures)
    actual = set(files)
    for unexpected in sorted(actual - expected, key=str):
        failures.append(f"path is not listed in manifest: {unexpected}")
    for missing in sorted(expected - actual, key=str):
        failures.append(f"manifest path is missing: {missing}")

    configured_denylist = args.denylist
    if configured_denylist is None:
        raw_denylist = os.environ.get("PRIVATE_DENYLIST_FILE", "").strip()
        configured_denylist = Path(raw_denylist) if raw_denylist else None
    denylist, denylist_failures = denylist_values(configured_denylist)
    failures.extend(denylist_failures)

    for relative, path in files.items():
        try:
            content = path.read_bytes()
        except OSError as exc:
            failures.append(f"cannot read file: {relative}: {exc}")
            continue
        failures.extend(scan_bytes(relative, content))
        lowered = content.decode("utf-8", errors="ignore").casefold()
        if any(value.casefold() in lowered for value in denylist):
            failures.append(f"private denylist match: {relative}")

    staged, staged_failures, git_scanned = staged_files(
        args.require_git or args.staged
    )
    failures.extend(staged_failures)
    for relative, content in staged.items():
        if relative not in expected:
            failures.append(f"staged path is not listed in manifest: {relative}")
        failures.extend(scan_bytes(relative, content))
        lowered = content.decode("utf-8", errors="ignore").casefold()
        if any(value.casefold() in lowered for value in denylist):
            failures.append(f"private denylist match in staged file: {relative}")

    if failures:
        print("Privacy scan failed:")
        for failure in sorted(set(failures)):
            print(f"- {failure}")
        return 1
    print(
        "Privacy scan passed: "
        f"{len(files)} manifest files, {len(staged)} staged files, "
        f"git_index_scanned={str(git_scanned).lower()}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
