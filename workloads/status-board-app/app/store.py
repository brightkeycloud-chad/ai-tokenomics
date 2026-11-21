"""DynamoDB access layer.

Every read and write goes through this module. Routes never call boto3
directly, and nothing in this codebase uses PartiQL (``execute_statement``):
the typed resource API sends values as parameters, so a value that came from a
request can never be concatenated into a statement.

Configuration comes from the environment so that the same image runs against
DynamoDB Local and against AWS without a code change.
"""

import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger(__name__)

SERVICES_TABLE = os.environ.get("SERVICES_TABLE", "status-board-services")
INCIDENTS_TABLE = os.environ.get("INCIDENTS_TABLE", "status-board-incidents")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

#: Set to something like ``http://localhost:8000`` to use DynamoDB Local.
#: Left unset in AWS, where boto3 resolves the real endpoint itself.
DYNAMODB_ENDPOINT_URL = os.environ.get("DYNAMODB_ENDPOINT_URL") or None

_resource = None


def _dynamodb():
    """Return a cached DynamoDB resource, building it on first use.

    Building it lazily keeps import time free of network and credential work,
    which matters for container start-up and lets tests install a mock first.
    """
    global _resource
    if _resource is None:
        logger.info(
            "creating DynamoDB resource region=%s endpoint=%s",
            AWS_REGION,
            DYNAMODB_ENDPOINT_URL or "default",
        )
        _resource = boto3.resource(
            "dynamodb",
            region_name=AWS_REGION,
            endpoint_url=DYNAMODB_ENDPOINT_URL,
        )
    return _resource


def reset_cache() -> None:
    """Drop the cached resource. Used by tests between mock backends."""
    global _resource
    _resource = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_services() -> list[dict]:
    """Return every service on the board, ordered by name."""
    table = _dynamodb().Table(SERVICES_TABLE)
    items = table.scan().get("Items", [])
    logger.info("listed %d services", len(items))
    return sorted(items, key=lambda item: item.get("name", ""))


def get_service(service_id: str) -> dict | None:
    """Return one service, or ``None`` when no service has that id."""
    table = _dynamodb().Table(SERVICES_TABLE)
    item = table.get_item(Key={"service_id": service_id}).get("Item")
    if item is None:
        logger.info("service %s not found", service_id)
    return item


def put_service(service_id: str, name: str, status: str) -> dict:
    """Create or replace a service record and return it."""
    item = {
        "service_id": service_id,
        "name": name,
        "status": status,
        "updated_at": _now(),
    }
    _dynamodb().Table(SERVICES_TABLE).put_item(Item=item)
    logger.info("stored service %s status=%s", service_id, status)
    return item


def list_incidents(service_id: str, limit: int = 10) -> list[dict]:
    """Return the most recent incidents for one service, newest first."""
    table = _dynamodb().Table(INCIDENTS_TABLE)
    response = table.query(
        KeyConditionExpression=Key("service_id").eq(service_id),
        ScanIndexForward=False,
        Limit=limit,
    )
    items = response.get("Items", [])
    logger.info("listed %d incidents for %s", len(items), service_id)
    return items


def put_incident(service_id: str, status: str, message: str) -> dict:
    """Record an incident update against a service and return it.

    The service's own status is updated to match, so the board and the incident
    log never disagree.
    """
    created_at = _now()
    incident_id = str(uuid.uuid4())
    item = {
        "service_id": service_id,
        # Sort key. The timestamp orders the history and the id keeps two
        # incidents recorded in the same instant from overwriting each other.
        "incident_key": f"{created_at}#{incident_id}",
        "incident_id": incident_id,
        "created_at": created_at,
        "status": status,
        "message": message,
    }
    _dynamodb().Table(INCIDENTS_TABLE).put_item(Item=item)

    table = _dynamodb().Table(SERVICES_TABLE)
    table.update_item(
        Key={"service_id": service_id},
        UpdateExpression="SET #s = :status, updated_at = :updated_at",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": status, ":updated_at": created_at},
    )
    logger.info("recorded incident for %s status=%s", service_id, status)
    return item
