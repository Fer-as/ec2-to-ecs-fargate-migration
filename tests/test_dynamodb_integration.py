"""Integration tests for STORAGE_BACKEND=dynamodb against DynamoDB Local.

Each test gets a fresh, seeded table. "Independent instances" are separate
imports of app.py, each with its own Flask app and its own boto3 client.
"""

import collections
import threading
import time

import pytest

from conftest import dynamodb_local_env, load_app_module, requires_dynamodb_local

pytestmark = requires_dynamodb_local

SEED = [
    {"id": 1, "name": "Widget A", "price": 29.99, "stock": 100},
    {"id": 2, "name": "Widget B", "price": 39.99, "stock": 50},
    {"id": 3, "name": "Widget C", "price": 49.99, "stock": 75},
]


def post_order(client, product_id, quantity):
    return client.post("/api/v1/orders", json={"product_id": product_id, "quantity": quantity})


def stock(instance, product_id):
    return instance.app.test_client().get(f"/api/v1/products/{product_id}").get_json()["stock"]


def set_stock(instance, product_id, value):
    instance.store.client.update_item(
        TableName=instance.store.table_name,
        Key={"pk": {"S": "PRODUCT"}, "sk": {"N": str(product_id)}},
        UpdateExpression="SET stock = :s", ExpressionAttributeValues={":s": {"N": str(value)}})


def raw_items(instance, pk):
    return list(instance.store._query_all(pk))


def last_order_id(instance):
    item = instance.store.client.get_item(
        TableName=instance.store.table_name,
        Key={"pk": {"S": "SEQUENCE"}, "sk": {"N": "0"}}, ConsistentRead=True)["Item"]
    return int(item["last_order_id"]["N"])


def run_concurrently(instances, requests):
    """Send (product_id, quantity) requests from parallel threads, spread over instances."""
    results = [None] * len(requests)
    barrier = threading.Barrier(len(requests))

    def worker(i, product_id, quantity):
        client = instances[i % len(instances)].app.test_client()
        barrier.wait()  # start all requests together
        r = post_order(client, product_id, quantity)
        results[i] = (r.status_code, r.get_json())

    threads = [threading.Thread(target=worker, args=(i, p, q)) for i, (p, q) in enumerate(requests)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


# 1 -------------------------------------------------------------------------------
def test_products_are_seeded_correctly(ddb_app):
    c = ddb_app().app.test_client()
    assert c.get("/api/v1/products").get_json() == {"products": SEED, "count": 3}
    assert c.get("/api/v1/products/1").get_json() == SEED[0]
    assert c.get("/api/v1/products/999").status_code == 404
    assert c.get("/api/v1/orders").get_json() == {"orders": [], "count": 0}


def test_seed_is_idempotent_and_never_resets_stock(ddb_app):
    a = ddb_app()
    assert post_order(a.app.test_client(), 1, 7).status_code == 201
    created, existing = a.store.seed()  # e.g. re-run during a later deployment
    assert created == []
    assert existing == ["sequence", "product 1", "product 2", "product 3"]
    assert stock(a, 1) == 93
    assert post_order(a.app.test_client(), 1, 1).get_json()["id"] == 2  # sequence kept


# 2 + 3 ---------------------------------------------------------------------------
def test_valid_order_succeeds_and_inventory_decreases(ddb_app):
    c = ddb_app().app.test_client()
    r = post_order(c, 1, 2)
    assert r.status_code == 201
    order = r.get_json()
    assert set(order) == {"id", "product_id", "product_name", "quantity", "total_price", "created_at"}
    assert (order["id"], order["product_id"], order["product_name"], order["quantity"], order["total_price"]) \
        == (1, 1, "Widget A", 2, 59.98)
    assert isinstance(order["id"], int)
    assert c.get("/api/v1/products/1").get_json()["stock"] == 98
    assert c.get("/api/v1/orders").get_json() == {"orders": [order], "count": 1}
    stats = c.get("/api/v1/stats").get_json()
    assert (stats["total_products"], stats["total_orders"], stats["total_revenue"]) == (3, 1, 59.98)


def test_money_is_exact(ddb_app):
    c = ddb_app().app.test_client()
    # In floating point 29.99 * 3 = 89.97000000000001; DynamoDB stores exact decimals.
    assert post_order(c, 1, 3).get_json()["total_price"] == 89.97
    assert post_order(c, 3, 1).get_json()["total_price"] == 49.99
    assert c.get("/api/v1/stats").get_json()["total_revenue"] == 139.96


# 4 -------------------------------------------------------------------------------
def test_orders_survive_application_restart(ddb_app):
    first = ddb_app()
    c = first.app.test_client()
    orders = [post_order(c, 1, 2).get_json(), post_order(c, 2, 5).get_json()]
    del first, c  # "stop" the first process

    restarted = ddb_app()
    c = restarted.app.test_client()
    assert c.get("/api/v1/orders").get_json() == {"orders": orders, "count": 2}
    assert c.get("/api/v1/products/1").get_json()["stock"] == 98
    assert c.get("/api/v1/products/2").get_json()["stock"] == 45
    assert post_order(c, 1, 1).get_json()["id"] == 3  # IDs continue, no reuse


# 5 + 6 ---------------------------------------------------------------------------
def test_two_instances_share_orders_and_stock(ddb_app):
    a, b = ddb_app(), ddb_app()
    assert a.store is not b.store and a.store.client is not b.store.client
    ca, cb = a.app.test_client(), b.app.test_client()

    order_a = post_order(ca, 1, 5).get_json()
    order_b = post_order(cb, 1, 3).get_json()
    assert (order_a["id"], order_b["id"]) == (1, 2)  # no duplicate IDs across instances

    assert ca.get("/api/v1/orders").get_json() == cb.get("/api/v1/orders").get_json()
    assert ca.get("/api/v1/orders").get_json()["count"] == 2
    assert stock(a, 1) == stock(b, 1) == 92
    assert ca.get("/api/v1/stats").get_json()["total_orders"] == 2
    assert cb.get("/api/v1/stats").get_json()["total_orders"] == 2


# 7 -------------------------------------------------------------------------------
def test_concurrent_orders_cannot_oversell(ddb_app):
    instances = [ddb_app() for _ in range(4)]
    set_stock(instances[0], 2, 5)

    results = run_concurrently(instances, [(2, 1)] * 20)
    statuses = collections.Counter(status for status, _ in results)
    assert statuses == {201: 5, 400: 15}, results
    assert all(body == {"error": "Insufficient stock"} for status, body in results if status == 400)
    assert stock(instances[1], 2) == 0
    product_2_orders = [o for o in raw_items(instances[2], "ORDER") if o["product_id"] == 2]
    assert len(product_2_orders) == 5


def test_concurrent_multi_unit_orders_cannot_oversell(ddb_app):
    instances = [ddb_app() for _ in range(4)]
    set_stock(instances[0], 3, 10)
    results = run_concurrently(instances, [(3, 3)] * 12)  # 36 units wanted, 10 available
    sold = sum(body["quantity"] for status, body in results if status == 201)
    assert sold == 9  # three orders of 3 fit; a fourth would oversell
    assert stock(instances[0], 3) == 1
    assert all(status in (201, 400) for status, _ in results), results


# 8 -------------------------------------------------------------------------------
def test_order_ids_are_unique_under_concurrency(ddb_app):
    instances = [ddb_app() for _ in range(4)]
    results = run_concurrently(instances, [(1, 1)] * 40)
    assert all(status == 201 for status, _ in results), results
    ids = [body["id"] for _, body in results]
    assert len(set(ids)) == 40
    assert all(isinstance(i, int) for i in ids)
    listed = instances[0].app.test_client().get("/api/v1/orders").get_json()
    assert listed["count"] == 40 and sorted(o["id"] for o in listed["orders"]) == sorted(ids)
    assert stock(instances[3], 1) == 60


# 9 -------------------------------------------------------------------------------
@pytest.mark.parametrize("payload, status", [
    ({"product_id": 1, "quantity": -1}, 400),
    ({"product_id": 1, "quantity": 0}, 400),
    ({"product_id": 1, "quantity": "abc"}, 400),
    ({"product_id": 999, "quantity": 1}, 404),
    ({"product_id": 1, "quantity": 101}, 400),
])
def test_invalid_orders_do_not_modify_inventory(ddb_app, payload, status):
    a = ddb_app()
    c = a.app.test_client()
    assert c.post("/api/v1/orders", json=payload).status_code == status
    assert [p["stock"] for p in c.get("/api/v1/products").get_json()["products"]] == [100, 50, 75]
    assert raw_items(a, "ORDER") == []
    assert last_order_id(a) == 0  # rejected before an ID is used


# 10 ------------------------------------------------------------------------------
def test_failed_transaction_leaves_no_partial_order(ddb_app, monkeypatch):
    a = ddb_app()
    set_stock(a, 3, 2)
    # Simulate a race: this instance read stock 75 just before another instance
    # sold it down to 2. The pre-check passes, so the transaction must stop it.
    stale = {"id": 3, "name": "Widget C", "price": 49.99, "stock": 75}
    monkeypatch.setattr(a.store, "get_product", lambda product_id: dict(stale))

    r = post_order(a.app.test_client(), 3, 5)
    assert r.status_code == 400 and r.get_json() == {"error": "Insufficient stock"}

    b = ddb_app()  # independent reader
    assert stock(b, 3) == 2                 # stock untouched
    assert raw_items(b, "ORDER") == []      # no order record written
    assert last_order_id(b) == 1            # the attempt used ID 1 (gap), but nothing was saved


def test_existing_order_is_never_overwritten(ddb_app):
    a = ddb_app()
    # Simulate a sequence that is behind the data (e.g. a bad manual restore).
    a.store.client.put_item(TableName=a.store.table_name, Item={
        "pk": {"S": "ORDER"}, "sk": {"N": "1"}, "product_id": {"N": "2"},
        "product_name": {"S": "Widget B"}, "quantity": {"N": "4"},
        "total_price": {"N": "159.96"}, "created_at": {"S": "2026-01-01T00:00:00"}})

    r = post_order(a.app.test_client(), 1, 1)
    assert r.status_code == 201 and r.get_json()["id"] == 2
    orders = {o["id"]: o for o in a.app.test_client().get("/api/v1/orders").get_json()["orders"]}
    assert orders[1]["product_name"] == "Widget B" and orders[1]["quantity"] == 4  # untouched
    assert stock(a, 1) == 99  # decremented once, not twice


# Transaction conflicts ----------------------------------------------------------------
# DynamoDB Local serialises transactions, so it never returns TransactionConflict.
# Real DynamoDB does under contention. These tests inject that cancellation on the
# transaction call only; everything else (stock, orders, sequence) is real DynamoDB Local.

def _conflict_error():
    from botocore.exceptions import ClientError
    return ClientError({
        "Error": {"Code": "TransactionCanceledException", "Message": "Transaction cancelled"},
        "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}],
    }, "TransactWriteItems")


def _inject_conflicts(monkeypatch, store, count):
    real = store.client.transact_write_items
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] <= count:
            raise _conflict_error()
        return real(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", flaky)
    return calls


def test_transaction_conflict_is_retried(ddb_app, monkeypatch):
    a = ddb_app()
    calls = _inject_conflicts(monkeypatch, a.store, count=2)
    r = post_order(a.app.test_client(), 1, 4)
    assert r.status_code == 201
    assert calls["n"] == 3
    assert r.get_json()["id"] == 3  # IDs 1 and 2 were used by the cancelled attempts
    assert stock(a, 1) == 96        # decremented exactly once
    assert [o["sk"] for o in raw_items(a, "ORDER")] == [3]


def test_persistent_conflicts_return_503_without_partial_state(ddb_app, monkeypatch):
    a = ddb_app()
    _inject_conflicts(monkeypatch, a.store, count=10**6)
    r = post_order(a.app.test_client(), 1, 4)
    assert r.status_code == 503
    assert r.get_json() == {"error": "Service temporarily unavailable, please retry"}
    assert stock(a, 1) == 100
    assert raw_items(a, "ORDER") == []


# /ready -----------------------------------------------------------------------------
def test_ready_when_seeded_and_503_when_not(ddb_app):
    a = ddb_app()
    c = a.app.test_client()
    assert c.get("/ready").status_code == 200
    assert c.get("/ready").get_json() == {"status": "ready"}
    a.store.client.delete_item(TableName=a.store.table_name,
                               Key={"pk": {"S": "SEQUENCE"}, "sk": {"N": "0"}})
    r = c.get("/ready")
    assert r.status_code == 503 and r.get_json() == {"status": "not ready"}
    assert c.get("/health").status_code == 200  # liveness is unaffected


def test_ready_is_503_when_dynamodb_is_unreachable(monkeypatch):
    env = dynamodb_local_env("any-table")
    env["DYNAMODB_ENDPOINT_URL"] = "http://127.0.0.1:9"  # nothing listens here
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    c = load_app_module().app.test_client()
    started = time.monotonic()
    r = c.get("/ready")
    elapsed = time.monotonic() - started
    assert r.status_code == 503
    assert r.get_json() == {"status": "not ready"}  # no table name or error details
    assert elapsed < 5, f"/ready took {elapsed:.1f}s; must fit a load balancer health-check timeout"
    assert c.get("/health").status_code == 200
    r = c.get("/api/v1/products")
    assert r.status_code == 500 and r.get_json() == {"error": "Internal server error"}
