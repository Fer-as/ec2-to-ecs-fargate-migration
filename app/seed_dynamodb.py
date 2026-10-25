"""
Explicit, idempotent initialisation of the shared DynamoDB store.

Run once before the first deployment that uses STORAGE_BACKEND=dynamodb.
Re-running is safe: existing products, stock and the order sequence are
never overwritten. The application itself never seeds on start-up.

    DYNAMODB_TABLE_NAME=legacy-api AWS_REGION=eu-west-2 python seed_dynamodb.py

--create-table is only allowed with DYNAMODB_ENDPOINT_URL (DynamoDB Local).
In AWS the table is created by Terraform.
"""

import argparse
import logging
import os
import sys

from storage import DynamoDBStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("seed_dynamodb")


def create_table(client, table_name):
    """Create the table with the same key schema Terraform will use."""
    try:
        client.create_table(
            TableName=table_name,
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "N"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        logger.info("Created table %s", table_name)
    except client.exceptions.ResourceInUseException:
        logger.info("Table %s already exists", table_name)
    client.get_waiter("table_exists").wait(TableName=table_name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--create-table", action="store_true",
                        help="create the table first (DynamoDB Local only)")
    args = parser.parse_args(argv)

    store = DynamoDBStore.from_env()
    if args.create_table:
        if not os.environ.get("DYNAMODB_ENDPOINT_URL"):
            logger.error("--create-table is only allowed with DYNAMODB_ENDPOINT_URL (DynamoDB Local)")
            return 2
        create_table(store.client, store.table_name)

    created, existing = store.seed()
    logger.info("Seed complete. Created: %s. Already present (left unchanged): %s",
                ", ".join(created) or "none", ", ".join(existing) or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
