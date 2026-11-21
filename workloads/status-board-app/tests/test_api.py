"""Tests for the HTTP routes.

Every route this application exposes needs a test in this file.
"""


def test_healthz_reports_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_services_is_empty_on_a_fresh_board(client):
    response = client.get("/api/services")
    assert response.status_code == 200
    assert response.json() == []


def test_list_services_returns_seeded_services(client, seeded):
    response = client.get("/api/services")
    assert response.status_code == 200
    names = [service["name"] for service in response.json()]
    assert names == ["Checkout API", "Notifications"]


def test_get_service_returns_detail_with_incidents(client, seeded):
    response = client.get("/api/services/notifications")
    assert response.status_code == 200

    body = response.json()
    assert body["service"]["status"] == "degraded"
    assert body["incidents"][0]["message"] == "Email delivery is slow."


def test_get_service_rejects_a_malformed_id(client):
    response = client.get("/api/services/Not_An_Id")
    assert response.status_code == 400
    assert "not valid" in response.json()["detail"]


def test_get_service_returns_404_when_missing(client):
    response = client.get("/api/services/missing-one")
    assert response.status_code == 404


def test_board_renders_a_badge_for_each_service(client, seeded):
    response = client.get("/")
    assert response.status_code == 200

    html = response.text
    assert "Checkout API" in html
    assert "badge-operational" in html
    assert "badge-degraded" in html


def test_board_renders_an_empty_message_with_no_services(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "No services are registered" in response.text
