"""Table-driven tests for the input validators.

Each table row is one input that a client sent us, or could send us. When a
bug report shows a new bad input, add a row here, not a new test function.
The ids are short so that a failure is easy to find in the verbose report.
"""

import pytest

from app.validators import (
    MAX_MESSAGE_LENGTH,
    check_message,
    check_service_id,
    check_status,
)

# --------------------------------------------------------------------------
# Service ids
# --------------------------------------------------------------------------

#: (raw value, cleaned value). Every row must be accepted.
GOOD_SERVICE_IDS = [
    pytest.param("checkout-api", "checkout-api", id="checkout"),
    pytest.param("payments", "payments", id="payments"),
    pytest.param("search", "search", id="search"),
    pytest.param("notifications", "notifications", id="notify"),
    pytest.param("auth", "auth", id="auth"),
    pytest.param("auth-v2", "auth-v2", id="auth-v2"),
    pytest.param("cart", "cart", id="cart"),
    pytest.param("email-relay", "email-relay", id="email"),
    pytest.param("inventory", "inventory", id="inventory"),
    pytest.param("orders-api", "orders-api", id="orders"),
    pytest.param("pricing", "pricing", id="pricing"),
    pytest.param("queue-worker", "queue-worker", id="queue"),
    pytest.param("recommend", "recommend", id="recommend"),
    pytest.param("sms-gateway", "sms-gateway", id="sms"),
    pytest.param("status-board", "status-board", id="board"),
    pytest.param("a1b", "a1b", id="min-len"),
    pytest.param("a" + "b" * 30 + "c", "a" + "b" * 30 + "c", id="max-len"),
    pytest.param("api2", "api2", id="digit-end"),
    pytest.param("a--b", "a--b", id="double-dash"),
    pytest.param(" payments", "payments", id="lead-space"),
    pytest.param("payments ", "payments", id="trail-space"),
    pytest.param("\tsearch\n", "search", id="tab-newline"),
]

#: Raw values that must be rejected.
BAD_SERVICE_IDS = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("ab", id="too-short"),
    pytest.param("a" * 33, id="too-long"),
    pytest.param("Checkout-API", id="upper"),
    pytest.param("-leading", id="lead-dash"),
    pytest.param("trailing-", id="trail-dash"),
    pytest.param("1search", id="lead-digit"),
    pytest.param("has space", id="inner-space"),
    pytest.param("has_underscore", id="underscore"),
    pytest.param("../etc", id="traversal"),
    pytest.param("café", id="accent"),
    pytest.param("pay‑ments", id="nb-hyphen"),
    pytest.param("search;drop", id="semicolon"),
    pytest.param(123, id="int"),
    pytest.param(None, id="none"),
    pytest.param(["search"], id="list"),
]


@pytest.mark.parametrize(("raw", "cleaned"), GOOD_SERVICE_IDS)
def test_good_service_id(raw, cleaned):
    assert check_service_id(raw) == cleaned


@pytest.mark.parametrize("raw", BAD_SERVICE_IDS)
def test_bad_service_id(raw):
    with pytest.raises(ValueError):
        check_service_id(raw)


@pytest.mark.parametrize("raw", BAD_SERVICE_IDS)
def test_bad_service_id_names_field(raw):
    with pytest.raises(ValueError, match="service_id"):
        check_service_id(raw)


# --------------------------------------------------------------------------
# Statuses
# --------------------------------------------------------------------------

GOOD_STATUSES = [
    pytest.param("operational", "operational", id="operational"),
    pytest.param("degraded", "degraded", id="degraded"),
    pytest.param("down", "down", id="down"),
    pytest.param("Operational", "operational", id="title-op"),
    pytest.param("DEGRADED", "degraded", id="upper-deg"),
    pytest.param("Down", "down", id="title-down"),
    pytest.param("dOwN", "down", id="mixed-down"),
    pytest.param(" down", "down", id="lead-space"),
    pytest.param("down ", "down", id="trail-space"),
    pytest.param("\tdegraded\n", "degraded", id="tab-newline"),
    pytest.param("  OPERATIONAL  ", "operational", id="upper-padded"),
]

BAD_STATUSES = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("ok", id="ok"),
    pytest.param("up", id="up"),
    pytest.param("outage", id="outage"),
    pytest.param("unknown", id="unknown"),
    pytest.param("degradedd", id="typo"),
    pytest.param("do wn", id="inner-space"),
    pytest.param("down!", id="punct"),
    pytest.param("döwn", id="umlaut"),
    pytest.param(0, id="int"),
    pytest.param(None, id="none"),
    pytest.param(False, id="bool"),
    pytest.param(["down"], id="list"),
]


@pytest.mark.parametrize(("raw", "cleaned"), GOOD_STATUSES)
def test_good_status(raw, cleaned):
    assert check_status(raw) == cleaned


@pytest.mark.parametrize("raw", BAD_STATUSES)
def test_bad_status(raw):
    with pytest.raises(ValueError):
        check_status(raw)


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

GOOD_MESSAGES = [
    pytest.param("Latency is high.", "Latency is high.", id="sentence"),
    pytest.param("x", "x", id="one-char"),
    pytest.param("x" * MAX_MESSAGE_LENGTH, "x" * MAX_MESSAGE_LENGTH, id="at-limit"),
    pytest.param(" padded ", "padded", id="padded"),
    pytest.param("\nnewlines\n", "newlines", id="outer-newline"),
    pytest.param("line one\nline two", "line one\nline two", id="inner-newline"),
    pytest.param("café is slow", "café is slow", id="accent"),
    pytest.param("p99 > 2s", "p99 > 2s", id="symbols"),
    pytest.param("<b>bold</b>", "<b>bold</b>", id="html"),
    pytest.param("50% of requests fail", "50% of requests fail", id="percent"),
    pytest.param(" " + "y" * MAX_MESSAGE_LENGTH + " ", "y" * MAX_MESSAGE_LENGTH,
                 id="limit-padded"),
]

BAD_MESSAGES = [
    pytest.param("", id="empty"),
    pytest.param(" ", id="space"),
    pytest.param("\n\t", id="whitespace"),
    pytest.param("x" * (MAX_MESSAGE_LENGTH + 1), id="over-limit"),
    pytest.param("x" * (MAX_MESSAGE_LENGTH * 2), id="double-limit"),
    pytest.param(5, id="int"),
    pytest.param(None, id="none"),
    pytest.param(True, id="bool"),
    pytest.param(["text"], id="list"),
    pytest.param({"text": "x"}, id="dict"),
]


@pytest.mark.parametrize(("raw", "cleaned"), GOOD_MESSAGES)
def test_good_message(raw, cleaned):
    assert check_message(raw) == cleaned


@pytest.mark.parametrize("raw", BAD_MESSAGES)
def test_bad_message(raw):
    with pytest.raises(ValueError):
        check_message(raw)
