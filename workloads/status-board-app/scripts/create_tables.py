"""Create the two DynamoDB tables this application needs.

Used for local development against DynamoDB Local or moto. In AWS the tables
are created by Terraform, not by this script.

Usage:
    python -m scripts.create_tables
"""

import logging
import sys

import boto3
from botocore.exceptions import ClientError

from app import store

logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
logger = logging.getLogger("create-tables")

TABLES = [
    {
        "TableName": store.SERVICES_TABLE,
        "KeySchema": [{"AttributeName": "service_id", "KeyType": "HASH"}],
        "AttributeDefinitions": [
            {"AttributeName": "service_id", "AttributeType": "S"}
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
    {
        "TableName": store.INCIDENTS_TABLE,
        "KeySchema": [
            {"AttributeName": "service_id", "KeyType": "HASH"},
            {"AttributeName": "incident_key", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "service_id", "AttributeType": "S"},
            {"AttributeName": "incident_key", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    },
]


def main() -> int:
    """Create both tables, treating an existing table as success."""
    client = boto3.client(
        "dynamodb",
        region_name=store.AWS_REGION,
        endpoint_url=store.DYNAMODB_ENDPOINT_URL,
    )
    for spec in TABLES:
        try:
            client.create_table(**spec)
            logger.info("created table %s", spec["TableName"])
        except ClientError as error:
            if error.response["Error"]["Code"] == "ResourceInUseException":
                logger.info("table %s already exists", spec["TableName"])
            else:
                logger.error(
                    "could not create table %s: %s", spec["TableName"], error
                )
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
