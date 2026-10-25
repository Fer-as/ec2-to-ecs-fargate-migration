"""Regression tests for the input-validation and error-format fixes.

The first six tests were the Task 2 known-defect tests (xfail). They now pass,
so their xfail markers were removed.
"""

import pytest


def post_order(client, payload):
    return client.post("/api/v1/orders", json=payload)


def assert_nothing_changed(client):
    assert client.get("/api/v1/orders").get_json()["count"] == 0
    stocks = [p["stock"] for p in client.get("/api/v1/products").get_json()["products"]]
    assert stocks == [100, 50, 75]


# --- previously known defects ------------------------------------------------

def test_negative_quantity_is_rejected(client):
    r = post_order(client, {"product_id": 2, "quantity": -5})
    stock = client.get("/api/v1/products/2").get_json()["stock"]
    assert r.status_code == 400, f"got {r.status_code} {r.get_json()}; stock is now {stock}"
    assert stock == 50


def test_zero_quantity_is_rejected(client):
    r = post_order(client, {"product_id": 2, "quantity": 0})
    assert r.status_code == 400, f"got {r.status_code} {r.get_json()}"


@pytest.mark.parametrize("quantity", ["abc", "2", None, [1]])
def test_wrong_quantity_type_is_rejected_with_400(client, quantity):
    r = post_order(client, {"product_id": 1, "quantity": quantity})
    assert r.status_code == 400, f"got {r.status_code} {r.get_json()}"


# --- exact error responses and no side effects ---------------------------------

@pytest.mark.parametrize("quantity", [-5, 0, "abc", "2", None, [1], {"n": 1}, 1.5, True, False])
def test_invalid_quantity_returns_400_without_side_effects(client, quantity):
    r = post_order(client, {"product_id": 1, "quantity": quantity})
    assert r.status_code == 400
    assert r.content_type == "application/json"
    assert r.get_json() == {"error": "400 Bad Request: quantity must be a positive integer"}
    assert_nothing_changed(client)


@pytest.mark.parametrize("product_id", ["1", 1.0, [1], {"id": 1}, True])
def test_invalid_product_id_type_returns_400(client, product_id):
    r = post_order(client, {"product_id": product_id, "quantity": 1})
    assert r.status_code == 400
    assert r.get_json() == {"error": "400 Bad Request: product_id must be an integer"}
    assert_nothing_changed(client)


@pytest.mark.parametrize("body", [[1, 2], "text", 42])
def test_non_object_json_body_returns_400(client, body):
    r = client.post("/api/v1/orders", json=body)
    assert r.status_code == 400
    assert r.get_json() == {"error": "400 Bad Request: JSON body must be an object"}
    assert_nothing_changed(client)


@pytest.mark.parametrize("data", ["{bad", "", "\xff"])
def test_undecodable_json_returns_400(client, data):
    r = client.post("/api/v1/orders", data=data, content_type="application/json")
    assert r.status_code == 400
    assert r.get_json()["error"].startswith("400 Bad Request:")
    assert_nothing_changed(client)


@pytest.mark.parametrize("content_type", ["text/plain", None])
def test_non_json_content_type_returns_415(client, content_type):
    kwargs = {"data": '{"product_id": 1}'}
    if content_type:
        kwargs["content_type"] = content_type
    r = client.post("/api/v1/orders", **kwargs)
    assert r.status_code == 415
    assert r.content_type == "application/json"
    assert r.get_json()["error"].startswith("415 Unsupported Media Type:")
    assert_nothing_changed(client)


def test_invalid_quantity_on_unknown_product_is_a_400(client):
    # Input is validated before the product lookup.
    r = post_order(client, {"product_id": 999, "quantity": -1})
    assert r.status_code == 400


def test_valid_large_quantity_still_reports_insufficient_stock(client):
    r = post_order(client, {"product_id": 1, "quantity": 10**9})
    assert r.status_code == 400
    assert r.get_json() == {"error": "Insufficient stock"}


# --- 405 Method Not Allowed --------------------------------------------------

@pytest.mark.parametrize("method, path, allowed", [
    ("delete", "/api/v1/products/1", {"GET", "HEAD", "OPTIONS"}),
    ("post", "/api/v1/products", {"GET", "HEAD", "OPTIONS"}),
    ("put", "/api/v1/orders", {"GET", "HEAD", "OPTIONS", "POST"}),
    ("post", "/health", {"GET", "HEAD", "OPTIONS"}),
    ("delete", "/api/v1/stats", {"GET", "HEAD", "OPTIONS"}),
])
def test_wrong_method_returns_json_405_with_allow_header(client, method, path, allowed):
    r = getattr(client, method)(path)
    assert r.status_code == 405
    assert r.content_type == "application/json"
    assert r.get_json() == {"error": "Method not allowed"}
    assert set(m.strip() for m in r.headers["Allow"].split(",")) == allowed
