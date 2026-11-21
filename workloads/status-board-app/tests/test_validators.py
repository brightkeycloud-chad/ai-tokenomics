"""Tests for the input validators."""

import pytest

from app.validators import (
    MAX_MESSAGE_LENGTH,
    VALID_STATUSES,
    check_message,
    check_service_id,
    check_status,
)


@pytest.mark.parametrize(
    "value", ["checkout-api", "payments", "search", "notifications", "a1b"]
)
def test_check_service_id_accepts_valid_ids(value):
    assert check_service_id(value) == value


def test_check_service_id_strips_surrounding_space():
    assert check_service_id("  payments  ") == "payments"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ab",
        "Checkout-API",
        "-leading",
        "trailing-",
        "has space",
        "has_underscore",
        "x" * 40,
        123,
        None,
    ],
)
def test_check_service_id_rejects_invalid_ids(value):
    with pytest.raises(ValueError):
        check_service_id(value)


@pytest.mark.parametrize("value", VALID_STATUSES)
def test_check_status_accepts_known_statuses(value):
    assert check_status(value) == value


def test_check_status_is_case_insensitive():
    assert check_status("Operational") == "operational"


@pytest.mark.parametrize("value", ["outage", "ok", "", "unknown", 1, None])
def test_check_status_rejects_unknown_statuses(value):
    with pytest.raises(ValueError):
        check_status(value)


def test_check_message_accepts_and_strips():
    assert check_message("  Latency is high.  ") == "Latency is high."


@pytest.mark.parametrize("value", ["", "   ", "x" * (MAX_MESSAGE_LENGTH + 1), 5, None])
def test_check_message_rejects_invalid_messages(value):
    with pytest.raises(ValueError):
        check_message(value)
