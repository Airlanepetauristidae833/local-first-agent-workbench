def test_health_is_backward_compatible(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_service_registry_contains_api(client) -> None:
    response = client.get("/api/v1/system/services")
    assert response.status_code == 200
    document = response.json()
    assert document["count"] == 1
    assert document["items"][0]["name"] == "api"
    assert document["items"][0]["status"] == "healthy"


def test_root_is_not_exposed(client) -> None:
    assert client.get("/").status_code == 404
