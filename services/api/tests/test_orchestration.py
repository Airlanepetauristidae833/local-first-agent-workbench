from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.routers.orchestration import (
    _compact_search_query,
    _research_queries,
    _research_search_requests,
)
from app.schemas.orchestration import RouteRequest
from app.services.orchestration_service import OrchestrationService


def test_console_is_served(client) -> None:
    response = client.get("/console")
    assert response.status_code == 200
    assert "Local-First Agent Workbench" in response.text
    assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate"
    assert response.headers["pragma"] == "no-cache"


def test_local_plan_is_persisted_and_runs_after_approval(client) -> None:
    created = client.post(
        "/api/v1/orchestrator/plans",
        json={
            "prompt": "\u8bf7\u6574\u7406\u8fd9\u6bb5\u672c\u5730\u8bf4\u660e"
        },
    )
    assert created.status_code == 201
    plan = created.json()
    assert plan["connectors"] == ["local_model"]
    assert plan["status"] == "planned"

    completed = client.post(f"/api/v1/orchestrator/plans/{plan['id']}/approve")
    assert completed.status_code == 200
    document = completed.json()
    assert document["status"] == "completed"
    assert document["result"]["outcome"] == "local_model_response"
    assert document["result"]["response"] == (
        "reply: \u8bf7\u6574\u7406\u8fd9\u6bb5\u672c\u5730\u8bf4\u660e"
    )

    listing = client.get("/api/v1/orchestrator/plans")
    assert listing.status_code == 200
    assert listing.json()["count"] == 1


def test_online_and_codex_are_permissions_until_the_local_planner_requests_them(client) -> None:
    created = client.post(
        "/api/v1/orchestrator/plans",
        json={
            "prompt": (
                "\u67e5\u6700\u65b0\u6cd5\u89c4\u5e76\u4fee\u590d\u4ee3\u7801"
            ),
            "workspace_id": "sample-workspace",
            "allow_online": True,
            "allow_codex": True,
        },
    )
    assert created.status_code == 201
    plan = created.json()
    assert plan["connectors"] == [
        "local_model", "workspace_readonly", "web_research", "codex_handoff"
    ]

    completed = client.post(f"/api/v1/orchestrator/plans/{plan['id']}/approve")
    assert completed.status_code == 200
    document = completed.json()
    assert document["status"] == "completed"
    assert document["result"]["outcome"] == "local_model_response"


def test_codex_handoff_stays_pending_until_an_executor_completes_it(tmp_path) -> None:
    service = OrchestrationService(tmp_path / "orchestrator.sqlite3")
    plan = service.create_plan(
        RouteRequest(prompt="Implement the requested change", allow_codex=True)
    )
    service.approve(plan.id)

    pending = service.handoff(plan.id, {"outcome": "codex_handoff_pending"})

    assert pending.status == "handoff_pending"
    assert pending.result == {"outcome": "codex_handoff_pending"}

    running = service.start_handoff(plan.id, "test-worker")
    assert running.status == "codex_running"
    assert running.result["worker_id"] == "test-worker"
    assert running.result["attempt_id"]
    assert running.result["attempt_no"] == 1

    completed = service.finish_handoff(
        plan.id,
        {
            "success": True,
            "worker_id": "test-worker",
            "attempt_id": running.result["attempt_id"],
            "summary": "implemented and tested",
        },
    )
    assert completed.status == "completed"
    assert completed.result["outcome"] == "codex_completed"
    assert completed.result["codex_execution"]["success"] is True


def test_codex_heartbeat_and_complete_routes_require_the_attempt_token(client) -> None:
    service = OrchestrationService(get_settings().orchestrator_store_path)
    plan = service.create_plan(
        RouteRequest(prompt="Validate worker HTTP contract", allow_codex=True)
    )
    service.approve(plan.id)
    service.handoff(plan.id, {"outcome": "codex_handoff_pending"})
    running = service.start_handoff(plan.id, "route-worker")
    attempt_id = running.result["attempt_id"]

    heartbeat = client.post(
        f"/api/v1/orchestrator/plans/{plan.id}/codex/heartbeat",
        json={"worker_id": "route-worker", "attempt_id": attempt_id},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["result"]["attempt_id"] == attempt_id

    completed = client.post(
        f"/api/v1/orchestrator/plans/{plan.id}/codex/complete",
        json={
            "worker_id": "route-worker",
            "attempt_id": attempt_id,
            "success": True,
            "summary": "done",
        },
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    duplicate = client.post(
        f"/api/v1/orchestrator/plans/{plan.id}/codex/complete",
        json={
            "worker_id": "route-worker",
            "attempt_id": attempt_id,
            "success": True,
            "summary": "done",
        },
    )
    assert duplicate.status_code == 200
    stale = client.post(
        f"/api/v1/orchestrator/plans/{plan.id}/codex/complete",
        json={
            "worker_id": "route-worker",
            "attempt_id": "stale-attempt",
            "success": True,
        },
    )
    assert stale.status_code == 409


def test_codex_handoff_lease_allows_takeover_and_rejects_stale_attempts(
    tmp_path,
) -> None:
    service = OrchestrationService(tmp_path / "orchestrator.sqlite3")
    plan = service.create_plan(
        RouteRequest(prompt="Implement safely", allow_codex=True)
    )
    service.approve(plan.id)
    service.handoff(plan.id, {"outcome": "codex_handoff_pending"})

    started_at = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    claimed = service.start_handoff(
        plan.id, "worker-a", now=started_at, lease_seconds=90
    )
    attempt_a = claimed.result["attempt_id"]
    renewed = service.heartbeat_handoff(
        plan.id,
        "worker-a",
        attempt_a,
        now=started_at + timedelta(seconds=30),
        lease_seconds=90,
    )
    assert renewed.result["heartbeat_at"] == (
        started_at + timedelta(seconds=30)
    ).isoformat()

    with pytest.raises(ValueError, match="active lease"):
        service.start_handoff(
            plan.id, "worker-a", now=started_at + timedelta(seconds=119)
        )
    with pytest.raises(ValueError, match="active lease"):
        service.start_handoff(
            plan.id, "worker-b", now=started_at + timedelta(seconds=119)
        )

    takeover = service.start_handoff(
        plan.id, "worker-b", now=started_at + timedelta(seconds=121)
    )
    attempt_b = takeover.result["attempt_id"]
    assert attempt_b != attempt_a
    assert takeover.result["attempt_no"] == 2

    with pytest.raises(ValueError, match="different attempt"):
        service.heartbeat_handoff(
            plan.id,
            "worker-a",
            attempt_a,
            now=started_at + timedelta(seconds=122),
        )
    with pytest.raises(ValueError, match="different attempt"):
        service.finish_handoff(
            plan.id,
            {
                "success": True,
                "worker_id": "worker-a",
                "attempt_id": attempt_a,
            },
            now=started_at + timedelta(seconds=122),
        )

    execution = {
        "success": True,
        "worker_id": "worker-b",
        "attempt_id": attempt_b,
        "summary": "taken over safely",
    }
    completed = service.finish_handoff(
        plan.id, execution, now=started_at + timedelta(seconds=123)
    )
    assert completed.status == "completed"
    # A lost HTTP acknowledgement can safely resend the same durable payload.
    assert (
        service.finish_handoff(
            plan.id, execution, now=started_at + timedelta(seconds=124)
        ).result["codex_execution"]["summary"]
        == "taken over safely"
    )


def test_expired_attempt_cannot_heartbeat_or_finish_without_takeover(tmp_path) -> None:
    service = OrchestrationService(tmp_path / "orchestrator.sqlite3")
    plan = service.create_plan(
        RouteRequest(prompt="Implement with a lease", allow_codex=True)
    )
    service.approve(plan.id)
    service.handoff(plan.id, {"outcome": "codex_handoff_pending"})
    started_at = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    claimed = service.start_handoff(
        plan.id, "worker-a", now=started_at, lease_seconds=10
    )
    attempt_id = claimed.result["attempt_id"]

    with pytest.raises(ValueError, match="lease expired"):
        service.heartbeat_handoff(
            plan.id,
            "worker-a",
            attempt_id,
            now=started_at + timedelta(seconds=11),
        )
    with pytest.raises(ValueError, match="lease expired"):
        service.finish_handoff(
            plan.id,
            {
                "success": True,
                "worker_id": "worker-a",
                "attempt_id": attempt_id,
            },
            now=started_at + timedelta(seconds=11),
        )


def test_stale_plan_transition_cannot_overwrite_newer_status(tmp_path) -> None:
    service = OrchestrationService(tmp_path / "orchestrator.sqlite3")
    created = service.create_plan(RouteRequest(prompt="Protect stale transitions"))
    stale = service.get_plan(created.id)
    assert stale is not None

    approved = service.approve(created.id)
    assert approved.status == "approved"
    with pytest.raises(ValueError, match="changed concurrently"):
        service._replace(stale, status="completed", result={"stale": True})
    assert service.get_plan(created.id).status == "approved"

    with pytest.raises(ValueError, match="already exists"):
        service._save(created)


def test_research_uses_reliable_engine_groups() -> None:
    assert _research_search_requests("OpenAI Codex latest updates")[0] == (
        "OpenAI Codex",
        "bing",
    )
    assert _compact_search_query("site:openai.com Codex latest updates") == (
        "openai Codex"
    )
    assert _research_search_requests("!github obsidian plugin") == [
        ("obsidian plugin", "github")
    ]
    assert _research_search_requests("latest scientific papers")[0][1] == (
        "openalex,crossref,arxiv"
    )
    assert _research_search_requests("research latest Codex updates")[0][1] == (
        "bing"
    )
    assert _research_queries(
        "Research the latest OpenAI Codex updates",
        "AI watches",
        ["site:openai.com Codex latest updates"],
    )[0] == "site:openai.com Codex latest updates"
