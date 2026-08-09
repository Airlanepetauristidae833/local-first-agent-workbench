import json

from app.config import get_settings


def test_workspace_listing_only_returns_registered_directories(client) -> None:
    response = client.get("/api/v1/workspaces")
    assert response.status_code == 200
    document = response.json()
    assert document["count"] == 1
    assert document["items"][0] == {
        "id": "sample-workspace",
        "name": "Sample Workspace",
        "description": "Test-only read-only workspace",
        "directory": "sample-workspace",
        "capabilities": ["inspect", "search"],
        "policy": {
            "file_access": "read-only",
            "command_execution": "disabled",
            "command_allowlist": [],
            "human_approval_required_for": ["write", "command"],
        },
    }


def test_workspace_inspection_is_metadata_only_and_ignores_dependencies(client) -> None:
    response = client.get("/api/v1/workspaces/sample-workspace/inspect")
    assert response.status_code == 200
    document = response.json()
    assert document["workspace"]["id"] == "sample-workspace"
    assert document["file_count"] == 2
    assert document["directory_count"] == 1
    assert document["truncated"] is False
    assert document["top_level_entries"] == ["README.md", "node_modules", "src"]
    assert {item["extension"]: item["files"] for item in document["extensions"]} == {
        ".md": 1,
        ".py": 1,
    }
    assert "node_modules" in document["ignored_directories"]


def test_workspace_inspection_can_be_bounded(client) -> None:
    response = client.get(
        "/api/v1/workspaces/sample-workspace/inspect",
        params={"max_files": 1},
    )
    assert response.status_code == 200
    document = response.json()
    assert document["file_count"] == 1
    assert document["truncated"] is True


def test_unknown_workspace_returns_404(client) -> None:
    assert client.get("/api/v1/workspaces/missing").status_code == 404
    assert client.get("/api/v1/workspaces/missing/inspect").status_code == 404
    assert (
        client.get("/api/v1/workspaces/missing/search", params={"q": "sample"}).status_code
        == 404
    )


def test_workspace_search_returns_bounded_line_snippets(client) -> None:
    response = client.get(
        "/api/v1/workspaces/sample-workspace/search",
        params={"q": "READY"},
    )
    assert response.status_code == 200
    document = response.json()
    assert document["workspace"]["id"] == "sample-workspace"
    assert document["query"] == "READY"
    assert document["case_sensitive"] is False
    assert document["files_scanned"] == 2
    assert document["matches"] == [
        {
            "path": "src/main.py",
            "line_number": 1,
            "snippet": "print('ready')",
        }
    ]
    assert document["truncated"] is False
    assert document["limits"]["max_results"] == 50
    assert ".py" in document["searched_extensions"]
    assert "node_modules" in document["ignored_directories"]


def test_workspace_search_respects_result_limit(client) -> None:
    response = client.get(
        "/api/v1/workspaces/sample-workspace/search",
        params={"q": "a", "max_results": 1},
    )
    assert response.status_code == 200
    document = response.json()
    assert len(document["matches"]) == 1
    assert document["truncated"] is True


def test_workspace_search_rejects_blank_query(client) -> None:
    response = client.get(
        "/api/v1/workspaces/sample-workspace/search",
        params={"q": "   "},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_search_query"


def test_workspace_search_requires_explicit_capability(client) -> None:
    workspace = get_settings().workspace_root / "inspect-only"
    workspace.mkdir()
    (workspace / ".ai-workspace.json").write_text(
        json.dumps(
            {
                "id": "inspect-only",
                "name": "Inspect Only",
                "capabilities": ["inspect"],
            }
        ),
        encoding="utf-8",
    )
    response = client.get(
        "/api/v1/workspaces/inspect-only/search",
        params={"q": "anything"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "workspace_capability_denied"


def test_workspace_search_skips_disallowed_and_oversized_files(client) -> None:
    sample = get_settings().workspace_root / "sample-workspace"
    (sample / "binary.bin").write_bytes(b"needle")
    (sample / "large.txt").write_text("needle" * 100, encoding="utf-8")
    response = client.get(
        "/api/v1/workspaces/sample-workspace/search",
        params={"q": "needle", "max_file_bytes": 32},
    )
    assert response.status_code == 200
    document = response.json()
    assert document["matches"] == []
    assert document["skipped_by_type"] == 1
    assert document["skipped_by_size"] == 1


def test_workspace_search_can_be_case_sensitive(client) -> None:
    response = client.get(
        "/api/v1/workspaces/sample-workspace/search",
        params={"q": "READY", "case_sensitive": True},
    )
    assert response.status_code == 200
    assert response.json()["matches"] == []
