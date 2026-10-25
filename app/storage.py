"""
Storage backends for the legacy API.

STORAGE_BACKEND=memory   (default) original in-process dicts. One process only.
STORAGE_BACKEND=dynamodb shared, durable state in one DynamoDB table, safe for
                         several Gunicorn workers, containers and ECS tasks.

DynamoDB table (created by Terraform; locally by seed_dynamodb.py --create-table):
  partition key  pk (String)   sort key  sk (Number)
  pk="PRODUCT"  sk=<product id>  name, price, stock
  pk="ORDER"    sk=<order id>    product_id, product_name, quantity, total_price, created_at
  pk="SEQUENCE" sk=0             last_order_id  (atomic counter for order IDs)
Prices and totals are stored as exact decimals; the API returns JSON numbers.
"""

import copy
import logging
import os
import random
import time
from decimal import Decimal

logger = logging.getLogger(__name__)

# Initial catalogue (the legacy app's hard-coded products)
SEED_PRODUCTS = [
    {"id": 1, "name": "Widget A", "price": "29.99", "stock": 100},
    {"id": 2, "name": "Widget B", "price": "39.99", "stock": 50},
    {"id": 3, "name": "Widget C", "price": "49.99", "stock": 75},
]


class ProductNotFound(Exception):
    pass


class InsufficientStock(Exception):
    pass


class StoreBusy(Exception):
    """Too much write contention; the client may retry."""


class StoreNotReady(Exception):
    """Storage is unreachable or not initialised."""


# ---------------------------------------------------------------------------
class MemoryStore:
    """The original legacy behaviour: state lives in this process only."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.products = {
            p["id"]: {"id": p["id"], "name": p["name"], "price": float(p["price"]), "stock": p["stock"]}
            for p in SEED_PRODUCTS
        }
        self.orders = []
        self.order_counter = 1

    def list_products(self):
        return list(self.products.values())

    def get_product(self, product_id):
        return self.products.get(product_id)

    def list_orders(self):
        return self.orders

    def create_order(self, product_id, quantity, created_at):
        product = self.products.get(product_id)
        if not product:
            raise ProductNotFound()
        if quantity > product["stock"]:
            raise InsufficientStock()
        order = {
            "id": self.order_counter,
            "product_id": product_id,
            "product_name": product["name"],
            "quantity": quantity,
            "total_price": product["price"] * quantity,
            "created_at": created_at,
        }
        self.orders.append(order)
        product["stock"] -= quantity
        self.order_counter += 1
        return order

    def stats(self):
        return len(self.products), len(self.orders), sum(o["total_price"] for o in self.orders)

    def check_ready(self):
        return None


# ---------------------------------------------------------------------------
PRODUCT = "PRODUCT"
ORDER = "ORDER"
SEQUENCE = "SEQUENCE"
MAX_TRANSACTION_ATTEMPTS = 5


def _money(value):
    """Exact Decimal in DynamoDB; a JSON float in the API, like the legacy app."""
    return float(value)


class DynamoDBStore:
    def __init__(self, table_name, client, ready_client=None):
        from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
        self.table_name = table_name
        self.client = client
        self.ready_client = ready_client or client
        self._ser = TypeSerializer().serialize
        self._deser = TypeDeserializer().deserialize

    @classmethod
    def from_env(cls):
        """Credentials come from the AWS SDK default provider chain (ECS task
        role, EC2 instance profile). The region comes from AWS_REGION or
        AWS_DEFAULT_REGION. DYNAMODB_ENDPOINT_URL is only for DynamoDB Local."""
        import boto3
        from botocore.config import Config

        table_name = os.environ.get("DYNAMODB_TABLE_NAME")
        if not table_name:
            raise RuntimeError("DYNAMODB_TABLE_NAME must be set when STORAGE_BACKEND=dynamodb")
        endpoint_url = os.environ.get("DYNAMODB_ENDPOINT_URL") or None
        # botocore only reads AWS_DEFAULT_REGION; accept AWS_REGION as well.
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            raise RuntimeError("AWS_REGION (or AWS_DEFAULT_REGION) must be set when STORAGE_BACKEND=dynamodb")
        if endpoint_url:
            logger.warning("Using DynamoDB endpoint override %s (local testing only)", endpoint_url)
        config = Config(connect_timeout=2, read_timeout=5,
                        retries={"mode": "standard", "max_attempts": 3})
        client = boto3.client("dynamodb", region_name=region, endpoint_url=endpoint_url, config=config)
        # Readiness must answer within a load balancer health-check timeout (~5s),
        # so it gets one short attempt instead of the normal retries.
        ready_config = Config(connect_timeout=1, read_timeout=1,
                              retries={"mode": "standard", "max_attempts": 1})
        ready_client = boto3.client("dynamodb", region_name=region, endpoint_url=endpoint_url,
                                    config=ready_config)
        return cls(table_name, client, ready_client)

    # -- helpers --------------------------------------------------------------
    def _key(self, pk, sk):
        return {"pk": {"S": pk}, "sk": {"N": str(sk)}}

    def _item(self, raw):
        return {k: self._deser(v) for k, v in raw.items()}

    def _query_all(self, pk):
        """Strongly consistent query of one partition, following pagination."""
        kwargs = {
            "TableName": self.table_name,
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": pk}},
            "ConsistentRead": True,
        }
        while True:
            page = self.client.query(**kwargs)
            for raw in page.get("Items", []):
                yield self._item(raw)
            if "LastEvaluatedKey" not in page:
                return
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    @staticmethod
    def _product_json(item):
        return {"id": int(item["sk"]), "name": item["name"],
                "price": _money(item["price"]), "stock": int(item["stock"])}

    @staticmethod
    def _order_json(item):
        return {"id": int(item["sk"]), "product_id": int(item["product_id"]),
                "product_name": item["product_name"], "quantity": int(item["quantity"]),
                "total_price": _money(item["total_price"]), "created_at": item["created_at"]}

    # -- reads ----------------------------------------------------------------
    def list_products(self):
        return [self._product_json(i) for i in self._query_all(PRODUCT)]

    def get_product(self, product_id):
        resp = self.client.get_item(TableName=self.table_name, Key=self._key(PRODUCT, product_id),
                                    ConsistentRead=True)
        return self._product_json(self._item(resp["Item"])) if "Item" in resp else None

    def list_orders(self):
        return [self._order_json(i) for i in self._query_all(ORDER)]

    # -- writes ---------------------------------------------------------------
    def _next_order_id(self):
        """Atomic counter: every call returns a new, unique integer.
        An ID is used up even if the order then fails, so IDs can have gaps."""
        resp = self.client.update_item(
            TableName=self.table_name,
            Key=self._key(SEQUENCE, 0),
            UpdateExpression="ADD last_order_id :one",
            ConditionExpression="attribute_exists(pk)",  # store must be seeded
            ExpressionAttributeValues={":one": {"N": "1"}},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["last_order_id"]["N"])

    def create_order(self, product_id, quantity, created_at):
        from botocore.exceptions import ClientError

        product = self.get_product(product_id)
        if product is None:
            raise ProductNotFound()
        if quantity > product["stock"]:
            raise InsufficientStock()  # fast path; the transaction re-checks atomically

        price = Decimal(str(product["price"]))
        total = price * quantity
        for attempt in range(1, MAX_TRANSACTION_ATTEMPTS + 1):
            order_id = self._next_order_id()
            order = {
                "pk": ORDER, "sk": order_id, "product_id": product_id,
                "product_name": product["name"], "quantity": quantity,
                "total_price": total, "created_at": created_at,
            }
            try:
                # Both writes succeed or neither does.
                self.client.transact_write_items(TransactItems=[
                    {"Update": {
                        "TableName": self.table_name,
                        "Key": self._key(PRODUCT, product_id),
                        "UpdateExpression": "SET stock = stock - :q",
                        "ConditionExpression": "attribute_exists(pk) AND stock >= :q",
                        "ExpressionAttributeValues": {":q": {"N": str(quantity)}},
                    }},
                    {"Put": {
                        "TableName": self.table_name,
                        "Item": {k: self._ser(v) for k, v in order.items()},
                        "ConditionExpression": "attribute_not_exists(pk)",  # never overwrite
                    }},
                ])
                return self._order_json(order)
            except ClientError as e:
                if e.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
                codes = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
                if codes and codes[0] == "ConditionalCheckFailed":
                    if self.get_product(product_id) is None:
                        raise ProductNotFound()
                    raise InsufficientStock()
                if len(codes) > 1 and codes[1] == "ConditionalCheckFailed":
                    logger.error("Order id %s already exists; sequence is behind. Retrying with a new id.", order_id)
                elif "TransactionConflict" not in codes:
                    raise
                logger.warning("Order transaction attempt %s/%s cancelled (%s); retrying",
                               attempt, MAX_TRANSACTION_ATTEMPTS, codes)
                time.sleep(random.uniform(0, 0.05 * 2 ** attempt))  # jittered backoff
        raise StoreBusy()

    def stats(self):
        """(total products, total orders, total revenue), summed exactly as Decimal."""
        total_products = sum(1 for _ in self._query_all(PRODUCT))
        total_orders, revenue = 0, Decimal("0")
        for item in self._query_all(ORDER):
            total_orders += 1
            revenue += item["total_price"]
        return total_products, total_orders, (float(revenue) if total_orders else 0)

    def check_ready(self):
        """Table reachable, permissions work and the store has been seeded."""
        resp = self.ready_client.get_item(TableName=self.table_name, Key=self._key(SEQUENCE, 0),
                                          ConsistentRead=True)
        if "Item" not in resp:
            raise StoreNotReady("order sequence item missing: run the seed step")

    # -- explicit, idempotent initialisation (never called at app start-up) ----
    def seed(self, products=None):
        """Create the catalogue and the order sequence only if they do not exist.
        Existing items (and their stock) are never overwritten."""
        from botocore.exceptions import ClientError

        created, existing = [], []
        items = [({"pk": SEQUENCE, "sk": 0, "last_order_id": 0}, "sequence")]
        for p in copy.deepcopy(products or SEED_PRODUCTS):
            items.append(({"pk": PRODUCT, "sk": p["id"], "name": p["name"],
                           "price": Decimal(str(p["price"])), "stock": p["stock"]},
                          f"product {p['id']}"))
        for item, label in items:
            try:
                self.client.put_item(TableName=self.table_name,
                                     Item={k: self._ser(v) for k, v in item.items()},
                                     ConditionExpression="attribute_not_exists(pk)")
                created.append(label)
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                existing.append(label)
        return created, existing


def create_store():
    backend = os.environ.get("STORAGE_BACKEND", "memory").strip().lower()
    if backend == "memory":
        return MemoryStore()
    if backend == "dynamodb":
        return DynamoDBStore.from_env()
    raise RuntimeError(f"Unsupported STORAGE_BACKEND '{backend}' (use 'memory' or 'dynamodb')")
