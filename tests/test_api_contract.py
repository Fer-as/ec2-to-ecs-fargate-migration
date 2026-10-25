"""Contract tests: record the inherited behaviour of the legacy API exactly."""

from datetime import datetime

import pytest

SEED_PRODUCTS = [
    {"id": 1, "name": "Widget A", "price": 29.99, "stock": 100},
    {"id": 2, "name": "Widget B", "price": 39.99, "stock": 50},
    {"id": 3, "name": "Widget C", "price": 49.99, "stock": 75},
]


def post_order(client, payload):
    return client.post("/api/v1/orders", json=payload)


def assert_naive_iso_timestamp(value):
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is None  # legacy format has no timezone suffix


# --- /health -----------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.content_type == "application/json"
    body = r.get_json()
    assert set(body) == {"status", "timestamp", "service", "version", "environment"}
    assert body["status"] == "healthy"
    assert body["service"] == "legacy-api"
    assert body["version"] == "1.0.0"
    assert body["environment"] == "production"
    assert_naive_iso_timestamp(body["timestamp"])


# --- products ----------------------------------------------------------------

def test_list_products(client):
    r = client.get("/api/v1/products")
    assert r.status_code == 200
    assert r.content_type == "application/json"
    assert r.get_json() == {"products": SEED_PRODUCTS, "count": 3}


def test_get_product_1(client):
    r = client.get("/api/v1/products/1")
    assert r.status_code == 200
    assert r.get_json() == SEED_PRODUCTS[0]


def test_get_missing_product_returns_404(client):
    r = client.get("/api/v1/products/999")
    assert r.status_code == 404
    assert r.content_type == "application/json"
    assert r.get_json() == {"error": "Product not found"}


# --- orders ------------------------------------------------------------------

def test_list_orders_starts_empty(client):
    r = client.get("/api/v1/orders")
    assert r.status_code == 200
    assert r.get_json() == {"orders": [], "count": 0}


def test_create_order_valid(client):
    r = post_order(client, {"product_id": 1, "quantity": 2})
    assert r.status_code == 201
    assert r.content_type == "application/json"
    order = r.get_json()
    assert set(order) == {"id", "product_id", "product_name", "quantity", "total_price", "created_at"}
    assert order["id"] == 1
    assert order["product_id"] == 1
    assert order["product_name"] == "Widget A"
    assert order["quantity"] == 2
    assert order["total_price"] == 59.98
    assert_naive_iso_timestamp(order["created_at"])

    # Side effects: stock is reduced and the order is listed.
    assert client.get("/api/v1/products/1").get_json()["stock"] == 98
    assert client.get("/api/v1/orders").get_json() == {"orders": [order], "count": 1}


def test_order_ids_increment(client):
    first = post_order(client, {"product_id": 1, "quantity": 1}).get_json()
    second = post_order(client, {"product_id": 2, "quantity": 1}).get_json()
    assert (first["id"], second["id"]) == (1, 2)


def test_quantity_defaults_to_1(client):
    r = post_order(client, {"product_id": 3})
    assert r.status_code == 201
    assert r.get_json()["quantity"] == 1
    assert r.get_json()["total_price"] == 49.99


def test_order_for_entire_stock_is_allowed(client):
    r = post_order(client, {"product_id": 2, "quantity": 50})
    assert r.status_code == 201
    assert client.get("/api/v1/products/2").get_json()["stock"] == 0


def test_create_order_missing_product_id(client):
    r = post_order(client, {"quantity": 1})
    assert r.status_code == 400
    assert r.get_json() == {"error": "400 Bad Request: product_id is required"}


def test_create_order_empty_json_body(client):
    r = post_order(client, {})
    assert r.status_code == 400
    assert r.get_json() == {"error": "400 Bad Request: No JSON data provided"}


def test_create_order_malformed_json(client):
    r = client.post("/api/v1/orders", data="{bad", content_type="application/json")
    assert r.status_code == 400
    assert r.get_json()["error"].startswith("400 Bad Request:")


def test_create_order_invalid_product(client):
    r = post_order(client, {"product_id": 999, "quantity": 1})
    assert r.status_code == 404
    assert r.get_json() == {"error": "Product not found"}
    assert client.get("/api/v1/orders").get_json()["count"] == 0


def test_create_order_insufficient_stock(client):
    r = post_order(client, {"product_id": 1, "quantity": 101})
    assert r.status_code == 400
    assert r.get_json() == {"error": "Insufficient stock"}
    # Nothing changed.
    assert client.get("/api/v1/products/1").get_json()["stock"] == 100
    assert client.get("/api/v1/orders").get_json()["count"] == 0


# --- stats -------------------------------------------------------------------

def test_stats_initial(client):
    r = client.get("/api/v1/stats")
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {"total_products", "total_orders", "total_revenue", "timestamp"}
    assert body["total_products"] == 3
    assert body["total_orders"] == 0
    assert body["total_revenue"] == 0
    assert_naive_iso_timestamp(body["timestamp"])


def test_stats_after_orders(client):
    post_order(client, {"product_id": 1, "quantity": 2})  # 59.98
    post_order(client, {"product_id": 3, "quantity": 1})  # 49.99
    body = client.get("/api/v1/stats").get_json()
    assert body["total_orders"] == 2
    assert body["total_revenue"] == pytest.approx(109.97)


# --- unknown endpoints -------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/",
    "/does-not-exist",
    "/api/v1/products/abc",
    "/api/v2/products",
])
def test_unknown_endpoint_returns_json_404(client, path):
    r = client.get(path)
    assert r.status_code == 404
    assert r.content_type == "application/json"
    assert r.get_json() == {"error": "Not found"}
