"""Tests for the DynamoDB access layer."""

from app import store


def test_list_services_returns_empty_board(dynamodb):
    assert store.list_services() == []


def test_put_and_get_service(dynamodb):
    store.put_service("payments", "Payments", "operational")
    item = store.get_service("payments")
    assert item["name"] == "Payments"
    assert item["status"] == "operational"
    assert item["updated_at"]


def test_get_service_returns_none_when_missing(dynamodb):
    assert store.get_service("nope") is None


def test_list_services_is_ordered_by_name(dynamodb):
    store.put_service("search", "Search", "operational")
    store.put_service("checkout-api", "Checkout API", "operational")
    assert [s["name"] for s in store.list_services()] == ["Checkout API", "Search"]


def test_put_incident_records_and_updates_service_status(dynamodb):
    store.put_service("search", "Search", "operational")
    incident = store.put_incident("search", "down", "Index is unavailable.")

    assert incident["message"] == "Index is unavailable."
    assert incident["incident_id"]
    assert store.get_service("search")["status"] == "down"


def test_list_incidents_returns_newest_first(dynamodb):
    store.put_service("search", "Search", "operational")
    store.put_incident("search", "degraded", "first")
    store.put_incident("search", "down", "second")

    messages = [row["message"] for row in store.list_incidents("search")]
    assert messages == ["second", "first"]


def test_list_incidents_is_empty_for_quiet_service(dynamodb):
    store.put_service("payments", "Payments", "operational")
    assert store.list_incidents("payments") == []
