"""Input validation for every value that arrives from a request.

Routes must never validate inline. Import the ``check_*`` helpers from this
module instead, so that the rules live in one place and are covered by one set
of tests. Each helper returns the cleaned value or raises ``ValueError`` with a
message naming the field, the value it rejected, and what it expected.
"""

import re

#: The only service states the board knows how to render. Adding a value here
#: means adding a matching branch to ``app/templates/board.html``.
VALID_STATUSES = ("operational", "degraded", "down")

#: Lowercase DNS-style identifiers: ``checkout-api``, ``payments``, ``search``.
SERVICE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")

MAX_MESSAGE_LENGTH = 280


def check_service_id(value: object) -> str:
    """Return ``value`` as a valid service id, or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError(
            f"service_id must be a string, got {type(value).__name__}"
        )
    candidate = value.strip()
    if not SERVICE_ID_PATTERN.match(candidate):
        raise ValueError(
            f"service_id {candidate!r} is not valid: expected 3-32 lowercase "
            "letters, digits or hyphens, starting with a letter"
        )
    return candidate


def check_status(value: object) -> str:
    """Return ``value`` as a valid service status, or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError(f"status must be a string, got {type(value).__name__}")
    candidate = value.strip().lower()
    if candidate not in VALID_STATUSES:
        raise ValueError(
            f"status {candidate!r} is not valid: expected one of "
            f"{', '.join(VALID_STATUSES)}"
        )
    return candidate


def check_message(value: object) -> str:
    """Return ``value`` as a valid incident message, or raise ``ValueError``."""
    if not isinstance(value, str):
        raise ValueError(f"message must be a string, got {type(value).__name__}")
    candidate = value.strip()
    if not candidate:
        raise ValueError("message is required and cannot be blank")
    if len(candidate) > MAX_MESSAGE_LENGTH:
        raise ValueError(
            f"message is {len(candidate)} characters, "
            f"which is over the {MAX_MESSAGE_LENGTH} character limit"
        )
    return candidate
