"""Tests for the sample data loader.

The deployment workflow runs this on every release, so running it twice must
not leave the board holding duplicate incidents.
"""

from app import store
from scripts import seed


def test_seed_writes_the_sample_board(dynamodb):
    assert seed.main() == 0

    services = store.list_services()
    assert [s["service_id"] for s in services] == [
        "checkout-api",
        "notifications",
        "payments",
        "search",
    ]
    assert store.get_service("notifications")["status"] == "degraded"
    assert len(store.list_incidents("notifications")) == 1


def test_seed_is_safe_to_run_twice(dynamodb):
    assert seed.main() == 0
    assert seed.main() == 0

    # Four services, not eight, and one incident, not two.
    assert len(store.list_services()) == 4
    assert len(store.list_incidents("notifications")) == 1


def test_seed_reports_a_skip_on_the_second_run(dynamodb):
    written, skipped = seed.seed_incidents()
    assert (written, skipped) == (1, 0)

    written, skipped = seed.seed_incidents()
    assert (written, skipped) == (0, 1)
