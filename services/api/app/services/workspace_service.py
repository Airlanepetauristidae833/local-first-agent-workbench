from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from app.config import get_settings
from app.schemas.workspace import (
    WorkspaceCapability,
    WorkspaceExtensionStat,
    WorkspaceInfo,
    WorkspaceInspection,
    WorkspaceManifest,
    WorkspaceSearch,
    WorkspaceSearchLimits,
    WorkspaceSearchMatch,
)

MANIFEST_NAME = ".ai-workspace.json"
DEFAULT_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".idea",
        ".pytest_cache",
        ".venv",
        ".vscode",
        "__pycache__",
        "node_modules",
    }
)
SEARCHABLE_TEXT_EXTENSIONS = frozenset(
    {
        ".cfg",
        ".css",
        ".csv",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".ps1",
        ".py",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
MAX_SEARCH_SNIPPET_CHARS = 300


class WorkspaceNotFoundError(KeyError):
    pass


class WorkspaceCapabilityError(PermissionError):
    pass


class WorkspaceConfigurationError(RuntimeError):
    pass


class WorkspaceSearchQueryError(ValueError):
    pass


class WorkspaceService:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._logger = logging.getLogger("ai_workstation.workspace")

    def list_workspaces(self) -> list[WorkspaceInfo]:
        if not self._root.exists():
            return []
        workspaces: list[WorkspaceInfo] = []
        for directory in sorted(self._root.iterdir(), key=lambda item: item.name.lower()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                workspace = self._load_workspace(directory)
            except (
                json.JSONDecodeError,
                OSError,
                ValidationError,
                WorkspaceConfigurationError,
            ) as exc:
                self._logger.warning(
                    "Ignoring invalid workspace directory=%s error=%s",
                    directory.name,
                    exc,
                )
                continue
            if workspace is not None:
                workspaces.append(workspace)
        return workspaces

    def get_workspace(self, workspace_id: str) -> WorkspaceInfo:
        for workspace in self.list_workspaces():
            if workspace.id == workspace_id:
                return workspace
        raise WorkspaceNotFoundError(workspace_id)

    def inspect(self, workspace_id: str, max_files: int = 5000) -> WorkspaceInspection:
        workspace = self.get_workspace(workspace_id)
        if WorkspaceCapability.INSPECT not in workspace.capabilities:
            raise WorkspaceCapabilityError(
                f"workspace '{workspace_id}' does not allow inspect"
            )

        workspace_root = self._workspace_path(workspace.id)
        extension_files: Counter[str] = Counter()
        extension_bytes: Counter[str] = Counter()
        file_count = 0
        directory_count = 0
        total_bytes = 0
        truncated = False
        stack = [workspace_root]

        top_level_entries = sorted(
            entry.name
            for entry in workspace_root.iterdir()
            if entry.name != MANIFEST_NAME and not entry.is_symlink()
        )[:100]

        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.name == MANIFEST_NAME or entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in DEFAULT_IGNORED_DIRECTORIES:
                            continue
                        directory_count += 1
                        stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if file_count >= max_files:
                        truncated = True
                        stack.clear()
                        break
                    stat = entry.stat(follow_symlinks=False)
                    size = max(0, stat.st_size)
                    suffix = Path(entry.name).suffix.lower() or "<none>"
                    file_count += 1
                    total_bytes += size
                    extension_files[suffix] += 1
                    extension_bytes[suffix] += size

        extensions = [
            WorkspaceExtensionStat(
                extension=extension,
                files=count,
                bytes=extension_bytes[extension],
            )
            for extension, count in sorted(
                extension_files.items(),
                key=lambda item: (-item[1], item[0]),
            )[:50]
        ]
        return WorkspaceInspection(
            workspace=workspace,
            file_count=file_count,
            directory_count=directory_count,
            total_bytes=total_bytes,
            extensions=extensions,
            top_level_entries=top_level_entries,
            ignored_directories=sorted(DEFAULT_IGNORED_DIRECTORIES),
            truncated=truncated,
            max_files=max_files,
            inspected_at=datetime.now(timezone.utc),
        )

    def search(
        self,
        workspace_id: str,
        query: str,
        *,
        case_sensitive: bool = False,
        max_files: int = 1000,
        max_directories: int = 1000,
        max_results: int = 50,
        max_file_bytes: int = 262144,
        max_total_bytes: int = 5242880,
    ) -> WorkspaceSearch:
        workspace = self.get_workspace(workspace_id)
        if WorkspaceCapability.SEARCH not in workspace.capabilities:
            raise WorkspaceCapabilityError(
                f"workspace '{workspace_id}' does not allow search"
            )

        query = query.strip()
        if not query:
            raise WorkspaceSearchQueryError("query must not be blank")

        workspace_root = self._workspace_path(workspace.id)
        matches: list[WorkspaceSearchMatch] = []
        files_considered = 0
        directories_scanned = 0
        files_scanned = 0
        bytes_scanned = 0
        skipped_by_type = 0
        skipped_by_size = 0
        skipped_by_encoding = 0
        skipped_unreadable = 0
        truncated = False
        stack = [workspace_root]

        while stack:
            if directories_scanned >= max_directories:
                truncated = True
                break
            current = stack.pop()
            directories_scanned += 1
            try:
                entries = sorted(
                    os.scandir(current),
                    key=lambda entry: entry.name.lower(),
                )
            except OSError:
                truncated = True
                continue

            directories: list[Path] = []
            stop_search = False
            for entry in entries:
                if entry.name == MANIFEST_NAME or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in DEFAULT_IGNORED_DIRECTORIES:
                        directories.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if files_considered >= max_files:
                    truncated = True
                    stop_search = True
                    break
                files_considered += 1

                suffix = Path(entry.name).suffix.lower()
                if suffix not in SEARCHABLE_TEXT_EXTENSIONS:
                    skipped_by_type += 1
                    continue
                try:
                    size = max(0, entry.stat(follow_symlinks=False).st_size)
                except OSError:
                    skipped_unreadable += 1
                    continue
                if size > max_file_bytes:
                    skipped_by_size += 1
                    continue
                if bytes_scanned + size > max_total_bytes:
                    truncated = True
                    stop_search = True
                    break

                try:
                    with open(entry.path, "rb") as handle:
                        payload = handle.read(max_file_bytes + 1)
                except OSError:
                    skipped_unreadable += 1
                    continue
                if len(payload) > max_file_bytes:
                    skipped_by_size += 1
                    continue
                try:
                    content = payload.decode("utf-8-sig")
                except UnicodeDecodeError:
                    skipped_by_encoding += 1
                    continue

                files_scanned += 1
                bytes_scanned += len(payload)
                normalized_query = query if case_sensitive else query.casefold()
                for line_number, line in enumerate(content.splitlines(), start=1):
                    normalized_line = line if case_sensitive else line.casefold()
                    match_index = normalized_line.find(normalized_query)
                    if match_index < 0:
                        continue
                    matches.append(
                        WorkspaceSearchMatch(
                            path=Path(entry.path)
                            .relative_to(workspace_root)
                            .as_posix(),
                            line_number=line_number,
                            snippet=_bounded_snippet(line, match_index),
                        )
                    )
                    if len(matches) >= max_results:
                        truncated = True
                        stop_search = True
                        break
                if stop_search:
                    break

            if stop_search:
                stack.clear()
                break
            stack.extend(reversed(directories))

        skipped_files = (
            skipped_by_type
            + skipped_by_size
            + skipped_by_encoding
            + skipped_unreadable
        )
        return WorkspaceSearch(
            workspace=workspace,
            query=query,
            case_sensitive=case_sensitive,
            directories_scanned=directories_scanned,
            files_scanned=files_scanned,
            bytes_scanned=bytes_scanned,
            skipped_files=skipped_files,
            skipped_by_type=skipped_by_type,
            skipped_by_size=skipped_by_size,
            skipped_by_encoding=skipped_by_encoding,
            skipped_unreadable=skipped_unreadable,
            matches=matches,
            searched_extensions=sorted(SEARCHABLE_TEXT_EXTENSIONS),
            ignored_directories=sorted(DEFAULT_IGNORED_DIRECTORIES),
            limits=WorkspaceSearchLimits(
                max_files=max_files,
                max_directories=max_directories,
                max_results=max_results,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
                max_snippet_chars=MAX_SEARCH_SNIPPET_CHARS,
            ),
            truncated=truncated,
            searched_at=datetime.now(timezone.utc),
        )

    def _workspace_path(self, workspace_id: str) -> Path:
        root = self._root.resolve()
        candidate = (root / workspace_id).resolve()
        if candidate.parent != root:
            raise WorkspaceConfigurationError("workspace path escapes the configured root")
        if not candidate.is_dir() or candidate.is_symlink():
            raise WorkspaceNotFoundError(workspace_id)
        return candidate

    def _load_workspace(self, directory: Path) -> WorkspaceInfo | None:
        manifest_path = directory / MANIFEST_NAME
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return None
        manifest = WorkspaceManifest.model_validate(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
        if manifest.id != directory.name:
            raise WorkspaceConfigurationError(
                "manifest id must exactly match its directory name"
            )
        self._workspace_path(manifest.id)
        capabilities = list(dict.fromkeys(manifest.capabilities))
        return WorkspaceInfo(
            id=manifest.id,
            name=manifest.name,
            description=manifest.description,
            directory=manifest.id,
            capabilities=capabilities,
            policy=manifest.policy,
        )


def _bounded_snippet(line: str, match_index: int) -> str:
    line = line.replace("\x00", "\ufffd")
    if len(line) <= MAX_SEARCH_SNIPPET_CHARS:
        return line
    start = max(0, match_index - (MAX_SEARCH_SNIPPET_CHARS // 3))
    end = min(len(line), start + MAX_SEARCH_SNIPPET_CHARS)
    start = max(0, end - MAX_SEARCH_SNIPPET_CHARS)
    snippet = line[start:end]
    if start > 0:
        snippet = "\u2026" + snippet[1:]
    if end < len(line):
        snippet = snippet[:-1] + "\u2026"
    return snippet


@lru_cache
def get_workspace_service() -> WorkspaceService:
    return WorkspaceService(get_settings().workspace_root)
