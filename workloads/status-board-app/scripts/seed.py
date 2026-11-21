"""Load the board with a fixed set of sample services and incidents.

The data is deliberately the same on every run, so the board looks identical
each time the demonstration is rebuilt.

Running this repeatedly is safe. Services are written unconditionally, because
writing the same service twice leaves the same record. Incidents are appended
rather than replaced, so an incident already on the board is skipped instead of
being recorded a second time.

Usage:
    python -m scripts.seed
"""

import logging
import sys

from app import store

logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
logger = logging.getLogger("seed")

SERVICES = [
    ("checkout-api", "Checkout API", "operational"),
    ("payments", "Payments", "operational"),
    ("search", "Search", "operational"),
    ("notifications", "Notifications", "degraded"),
]

INCIDENTS = [
    ("notifications", "degraded", "Email delivery is running behind by a few minutes."),
]


def seed_services() -> int:
    """Write every sample service and return how many were written."""
    for service_id, name, status in SERVICES:
        store.put_service(service_id, name, status)
    return len(SERVICES)


def seed_incidents() -> tuple[int, int]:
    """Record the sample incidents, skipping any already on the board.

    Returns the number written and the number skipped.
    """
    written = skipped = 0
    for service_id, status, message in INCIDENTS:
        already = store.list_incidents(service_id, limit=50)
        if any(item.get("message") == message for item in already):
            logger.info("incident for %s already recorded, skipping", service_id)
            skipped += 1
            continue
        store.put_incident(service_id, status, message)
        written += 1
    return written, skipped


def main() -> int:
    """Write the sample data and report what was written."""
    try:
        services = seed_services()
        written, skipped = seed_incidents()
    except Exception:
        logger.exception("seeding failed")
        return 1

    logger.info(
        "seeded %d services, %d incidents written, %d already present",
        services,
        written,
        skipped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
