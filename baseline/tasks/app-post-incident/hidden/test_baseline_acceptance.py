"""Hidden acceptance tests for the app-post-incident baseline task.

The harness copies this file into tests/ after the agent stops, then runs the
full suite. The agent never sees it. The tests use the fixtures in
tests/conftest.py: a DynamoDB mock with no AWS account.
"""

URL = "/api/services/{}/incidents"


def test_post_incident_returns_201_and_the_incident(client, seeded):
    response = client.post(
        URL.format("checkout-api"),
        json={"status": "down", "message": "Checkout returns errors."},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["service_id"] == "checkout-api"
    assert body["status"] == "down"
    assert body["message"] == "Checkout returns errors."
    assert body["incident_id"]
    assert body["created_at"]


def test_post_incident_updates_the_service_and_its_history(client, seeded):
    client.post(
        URL.format("checkout-api"),
        json={"status": "degraded", "message": "Slow payments."},
    )
    detail = client.get("/api/services/checkout-api").json()
    assert detail["service"]["status"] == "degraded"
    assert detail["incidents"][0]["message"] == "Slow payments."


def test_post_incident_cleans_the_values(client, seeded):
    response = client.post(
        URL.format("checkout-api"),
        json={"status": " Degraded ", "message": "  Slow payments.  "},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "degraded"
    assert response.json()["message"] == "Slow payments."


def test_post_incident_rejects_an_unknown_status(client, seeded):
    response = client.post(
        URL.format("checkout-api"),
        json={"status": "on-fire", "message": "Smoke."},
    )
    assert response.status_code == 400
    assert "status" in response.json()["detail"]


def test_post_incident_rejects_a_blank_message(client, seeded):
    response = client.post(
        URL.format("checkout-api"), json={"status": "down", "message": "   "}
    )
    assert response.status_code == 400
    assert "message" in response.json()["detail"]


def test_post_incident_rejects_a_long_message(client, seeded):
    response = client.post(
        URL.format("checkout-api"), json={"status": "down", "message": "x" * 281}
    )
    assert response.status_code == 400
    assert "280" in response.json()["detail"]


def test_post_incident_rejects_a_malformed_service_id(client, seeded):
    response = client.post(
        URL.format("Not_An_Id"), json={"status": "down", "message": "Errors."}
    )
    assert response.status_code == 400
    assert "service_id" in response.json()["detail"]


def test_post_incident_returns_404_for_an_unknown_service(client, seeded):
    response = client.post(
        URL.format("no-such-service"), json={"status": "down", "message": "Errors."}
    )
    assert response.status_code == 404


def test_post_incident_does_not_record_a_rejected_incident(client, seeded):
    client.post(URL.format("checkout-api"), json={"status": "on-fire", "message": "x"})
    detail = client.get("/api/services/checkout-api").json()
    assert detail["service"]["status"] == "operational"
    assert detail["incidents"] == []


def test_post_incident_declares_a_response_model(client):
    schema = client.get("/openapi.json").json()
    post = schema["paths"]["/api/services/{service_id}/incidents"]["post"]
    content = post["responses"]["201"]["content"]["application/json"]["schema"]
    assert "$ref" in content
