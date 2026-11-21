"""Shared fixtures.

Every test runs against a moto-mocked DynamoDB, so the suite needs no AWS
account and no network.
"""

import os

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")


@pytest.fixture
def dynamodb():
    """Start a mocked DynamoDB with both tables created and empty."""
    from app import store

    with mock_aws():
        store.reset_cache()
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=store.SERVICES_TABLE,
            KeySchema=[{"AttributeName": "service_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "service_id", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.create_table(
            TableName=store.INCIDENTS_TABLE,
            KeySchema=[
                {"AttributeName": "service_id", "KeyType": "HASH"},
                {"AttributeName": "incident_key", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "service_id", "AttributeType": "S"},
                {"AttributeName": "incident_key", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource
        store.reset_cache()


@pytest.fixture
def seeded(dynamodb):
    """A board holding two services, one of them degraded."""
    from app import store

    store.put_service("checkout-api", "Checkout API", "operational")
    store.put_service("notifications", "Notifications", "degraded")
    store.put_incident("notifications", "degraded", "Email delivery is slow.")
    return dynamodb


@pytest.fixture
def client(dynamodb):
    """A test client wired to the mocked tables."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
