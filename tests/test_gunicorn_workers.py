"""Shared state across Gunicorn worker processes.

Runs the real app under Gunicorn with 2 sync workers, once per storage backend:
  memory   - KNOWN DEFECT (kept as evidence): each worker has its own orders and
             stock, so these tests are expected to fail (strict xfail).
  dynamodb - the fix: both workers share DynamoDB (Local), so they must pass.
             Runs only when DYNAMODB_LOCAL_ENDPOINT is set.
Gunicorn needs a POSIX OS, so these tests are skipped on Windows.

To make the result deterministic, worker A is kept busy with a half-sent
request. The next order must then be served by worker B. When the held
request is completed, worker A creates its own order with the same ID.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from conftest import APP_DIR

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Gunicorn cannot run on Windows; runs on Linux/CI"
)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_healthy(base_url, proc, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"gunicorn exited early:\n{proc.stderr.read()}")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("gunicorn did not become healthy in time")


def _backends(defect_reason):
    """memory (known defect, strict xfail) and dynamodb (must pass)."""
    return pytest.mark.parametrize("gunicorn_server", [
        pytest.param("memory", marks=[
            pytest.mark.known_defect,
            pytest.mark.xfail(strict=True, raises=AssertionError, reason=defect_reason)]),
        "dynamodb",
    ], indirect=True)


@pytest.fixture
def gunicorn_server(request):
    if request.param == "dynamodb":
        request.getfixturevalue("ddb_table")  # fresh seeded table; sets STORAGE_BACKEND etc.
    port = _free_port()
    env = {k: v for k, v in os.environ.items()
           if k not in ("APP_NAME", "APP_VERSION", "ENVIRONMENT")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "gunicorn",
         "--bind", f"127.0.0.1:{port}",
         "--workers", "2", "--worker-class", "sync", "--timeout", "30",
         "wsgi:app"],
        cwd=APP_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_until_healthy(base_url, proc)
        yield port, base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _post_order(base_url, payload):
    req = urllib.request.Request(
        f"{base_url}/api/v1/orders", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def _expect_status(status, expected, what):
    # Setup problems must fail the test, not be counted as the expected defect
    # (the xfail only accepts AssertionError).
    if status != expected:
        raise RuntimeError(f"{what}: expected HTTP {expected}, got {status}")


def _while_worker_a_is_busy(port, method, path, body, action):
    """Hold worker A on a half-sent request, run `action` (served by worker B),
    then finish the held request. Returns (action result, status, JSON) from A."""
    held = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        held.sendall(f"{method} {path} HTTP/1.1\r\nHost: test\r\n".encode())
        time.sleep(0.5)
        result = action()
        held.sendall(
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n" + body
        )
        raw = b""
        while chunk := held.recv(65536):
            raw += chunk
    finally:
        held.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return result, status, json.loads(payload)


@_backends("Each Gunicorn worker has its own order counter; workers hand out duplicate order IDs")
def test_workers_share_order_ids(gunicorn_server):
    port, base_url = gunicorn_server
    body = json.dumps({"product_id": 1, "quantity": 1}).encode()

    (status_b, order_b), status_a, order_a = _while_worker_a_is_busy(
        port, "POST", "/api/v1/orders", body,
        lambda: _post_order(base_url, {"product_id": 1, "quantity": 1}),
    )
    _expect_status(status_b, 201, "order on worker B")
    _expect_status(status_a, 201, "order on worker A")

    # With shared state the second order would get the next ID.
    assert order_a["id"] == order_b["id"] + 1, (
        f"worker B created order id {order_b['id']}, "
        f"worker A then created order id {order_a['id']} (duplicate)"
    )


@_backends("Each Gunicorn worker has its own inventory; stock differs between workers")
def test_workers_share_inventory(gunicorn_server):
    port, base_url = gunicorn_server

    (status_b, _), status_a, product_on_a = _while_worker_a_is_busy(
        port, "GET", "/api/v1/products/1", b"",
        lambda: _post_order(base_url, {"product_id": 1, "quantity": 5}),
    )
    _expect_status(status_b, 201, "order on worker B")
    _expect_status(status_a, 200, "product read on worker A")

    # Worker B sold 5 of 100; with shared state worker A would report 95.
    assert product_on_a["stock"] == 95, (
        f"worker B sold 5 units, but worker A still reports stock {product_on_a['stock']}"
    )
