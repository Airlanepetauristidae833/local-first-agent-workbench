from app.main import app
from app.schemas.task import TaskCreate
from app.services.ollama_client import (
    OllamaUnavailableError,
    get_ollama_client,
)
from app.services.task_service import get_task_service


def test_task_creation_is_persisted(client) -> None:
    created = client.post(
        "/api/v1/tasks",
        json={"name": "ai.chat", "payload": {"message": "hello task"}},
    )
    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "queued"

    fetched = client.get(f"/api/v1/tasks/{task['id']}")
    assert fetched.status_code == 200
    document = fetched.json()
    assert document["name"] == "ai.chat"
    assert document["status"] == "completed"
    assert document["result"]["model"] == "test-model"
    assert document["result"]["response"] == "reply: hello task"
    assert document["started_at"] is not None
    assert document["completed_at"] is not None

    listing = client.get("/api/v1/tasks")
    assert listing.status_code == 200
    assert listing.json()["count"] == 1


def test_missing_task_returns_404(client) -> None:
    response = client.get("/api/v1/tasks/missing")
    assert response.status_code == 404


def test_unsupported_task_is_rejected(client) -> None:
    response = client.post(
        "/api/v1/tasks",
        json={"name": "shell.exec", "payload": {"command": "whoami"}},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unsupported_task"


def test_invalid_ai_task_payload_is_rejected(client) -> None:
    response = client.post(
        "/api/v1/tasks",
        json={"name": "ai.chat", "payload": {"message": "   "}},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_task_payload"


def test_workspace_inspect_task_is_persisted(client) -> None:
    created = client.post(
        "/api/v1/tasks",
        json={
            "name": "workspace.inspect",
            "payload": {"workspace_id": "sample-workspace"},
        },
    )
    assert created.status_code == 201
    task_id = created.json()["id"]
    fetched = client.get(f"/api/v1/tasks/{task_id}")
    assert fetched.status_code == 200
    document = fetched.json()
    assert document["status"] == "completed"
    assert document["result"]["workspace"]["id"] == "sample-workspace"
    assert document["result"]["file_count"] == 2


def test_workspace_search_task_is_persisted(client) -> None:
    created = client.post(
        "/api/v1/tasks",
        json={
            "name": "workspace.search",
            "payload": {
                "workspace_id": "sample-workspace",
                "query": "ready",
            },
        },
    )
    assert created.status_code == 201
    task_id = created.json()["id"]
    fetched = client.get(f"/api/v1/tasks/{task_id}")
    assert fetched.status_code == 200
    document = fetched.json()
    assert document["status"] == "completed"
    assert document["result"]["workspace"]["id"] == "sample-workspace"
    assert document["result"]["matches"][0]["path"] == "src/main.py"
    assert document["result"]["matches"][0]["line_number"] == 1


def test_failed_ai_task_persists_error(client) -> None:
    class UnavailableOllama:
        async def chat(self, message, model=None, system=None):
            raise OllamaUnavailableError("Ollama is unreachable")

    app.dependency_overrides[get_ollama_client] = UnavailableOllama
    created = client.post(
        "/api/v1/tasks",
        json={"name": "ai.chat", "payload": {"message": "hello"}},
    )
    task_id = created.json()["id"]
    fetched = client.get(f"/api/v1/tasks/{task_id}")
    assert fetched.status_code == 200
    document = fetched.json()
    assert document["status"] == "failed"
    assert document["error"] == "Ollama is unreachable"
    assert document["completed_at"] is not None


def test_interrupted_task_is_marked_failed(client) -> None:
    service = get_task_service()
    task = service.create(
        TaskCreate(name="ai.chat", payload={"message": "recover me"})
    )
    service.start(task.id)
    interrupted = service.fail_interrupted()
    assert [item.id for item in interrupted] == [task.id]
    recovered = service.get(task.id)
    assert recovered is not None
    assert recovered.status == "failed"
    assert recovered.error == "task interrupted by API restart"
