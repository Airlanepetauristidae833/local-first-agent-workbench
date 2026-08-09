from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.memory import get_memory_service, router
from app.services.memory_service import MemoryService


@pytest.fixture
def memory_client(tmp_path) -> Iterator[TestClient]:
    application = FastAPI()
    application.include_router(router)
    service = MemoryService(tmp_path / "personal-agent.sqlite3")
    application.dependency_overrides[get_memory_service] = lambda: service
    with TestClient(application) as client:
        yield client


def test_memory_rest_crud_list_and_search(memory_client: TestClient) -> None:
    created = memory_client.post(
        "/api/v1/memories",
        json={
            "scope": "project",
            "project_id": "paper",
            "kind": "episode",
            "content": "The literature review requires a PRISMA flow diagram.",
            "source": "workbench-session-summary",
            "source_ref": "session:abc",
            "confidence": 0.91,
            "metadata": {"session_id": "abc"},
        },
    )
    assert created.status_code == 201
    memory = created.json()
    assert memory["revision"] == 1
    assert memory["kind"] == "episode"

    listed = memory_client.get(
        "/api/v1/memories",
        params={"scope": "project", "project_id": "paper"},
    )
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["items"][0]["id"] == memory["id"]

    found = memory_client.get(
        "/api/v1/memories/search",
        params={
            "query": "literature review PRISMA",
            "project_id": "paper",
            "kinds": "episode",
        },
    )
    assert found.status_code == 200
    assert found.json()[0]["memory"]["id"] == memory["id"]
    assert "literature review" in found.json()[0]["matched_terms"]

    fetched = memory_client.get(f"/api/v1/memories/{memory['id']}")
    assert fetched.status_code == 200
    assert fetched.json() == memory

    updated = memory_client.patch(
        f"/api/v1/memories/{memory['id']}",
        params={"expected_revision": 1},
        json={
            "content": "The final literature review includes a PRISMA flow diagram.",
            "confidence": 0.97,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert updated.json()["confidence"] == 0.97

    deleted = memory_client.delete(
        f"/api/v1/memories/{memory['id']}",
        params={"expected_revision": 2},
    )
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert memory_client.get(f"/api/v1/memories/{memory['id']}").status_code == 404


def test_memory_rest_maps_conflicts_duplicates_and_missing_records(
    memory_client: TestClient,
) -> None:
    first = memory_client.post(
        "/api/v1/memories",
        json={
            "kind": "constraint",
            "content": "Never invent citations.",
            "source": "user",
        },
    ).json()
    second = memory_client.post(
        "/api/v1/memories",
        json={
            "kind": "constraint",
            "content": "Always report source identifiers.",
            "source": "user",
        },
    ).json()

    revision_conflict = memory_client.patch(
        f"/api/v1/memories/{first['id']}",
        params={"expected_revision": first["revision"] + 1},
        json={"confidence": 0.8},
    )
    assert revision_conflict.status_code == 409
    assert "concurrently" in revision_conflict.json()["detail"]

    duplicate_conflict = memory_client.patch(
        f"/api/v1/memories/{second['id']}",
        json={"content": "NEVER invent citations!"},
    )
    assert duplicate_conflict.status_code == 409
    assert duplicate_conflict.json()["detail"] == {
        "message": "the update would duplicate an existing memory",
        "existing_id": first["id"],
    }

    missing_id = "00000000-0000-0000-0000-000000000000"
    assert (
        memory_client.patch(
            f"/api/v1/memories/{missing_id}",
            json={"confidence": 0.5},
        ).status_code
        == 404
    )
    assert memory_client.delete(f"/api/v1/memories/{missing_id}").status_code == 404


def test_memory_rest_rejects_cross_scope_queries(memory_client: TestClient) -> None:
    response = memory_client.get(
        "/api/v1/memories/search",
        params={
            "query": "anything",
            "scope": "global",
            "project_id": "paper",
        },
    )

    assert response.status_code == 422
    assert "global scope" in response.json()["detail"]


def test_memory_list_and_search_support_stable_offset_pagination(
    memory_client: TestClient,
) -> None:
    for index in range(5):
        response = memory_client.post(
            "/api/v1/memories",
            json={
                "scope": "project",
                "project_id": "paper",
                "kind": "fact",
                "content": f"Pagination needle project fact {index}",
                "source": "test",
            },
        )
        assert response.status_code == 201

    first_list_page = memory_client.get(
        "/api/v1/memories",
        params={
            "scope": "project",
            "project_id": "paper",
            "kind": "fact",
            "limit": 2,
            "offset": 0,
        },
    ).json()["items"]
    second_list_page = memory_client.get(
        "/api/v1/memories",
        params={
            "scope": "project",
            "project_id": "paper",
            "kind": "fact",
            "limit": 2,
            "offset": 2,
        },
    ).json()["items"]
    assert len(first_list_page) == len(second_list_page) == 2
    assert {item["id"] for item in first_list_page}.isdisjoint(
        item["id"] for item in second_list_page
    )

    search_params = {
        "query": "pagination needle",
        "scope": "project",
        "project_id": "paper",
        "kinds": "fact",
    }
    full_search = memory_client.get(
        "/api/v1/memories/search",
        params={**search_params, "limit": 5},
    ).json()
    first_search_page = memory_client.get(
        "/api/v1/memories/search",
        params={**search_params, "limit": 2, "offset": 0},
    ).json()
    second_search_page = memory_client.get(
        "/api/v1/memories/search",
        params={**search_params, "limit": 2, "offset": 2},
    ).json()
    assert first_search_page + second_search_page == full_search[:4]
