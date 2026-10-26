#!/usr/bin/env python3
"""Offline migration rehearsal: real production containers sharing DynamoDB Local.

    python ci/local_migration_rehearsal.py [--image legacy-api:rehearsal] [--skip-build]

What it rehearses (all LOCAL; not an ALB, not AWS, not IAM):
  * "ecs" and "ec2" are two independent containers of the production image (Gunicorn,
    2 workers each, read-only root filesystem, no capabilities) with
    STORAGE_BACKEND=dynamodb, standing in for an ECS task and the DynamoDB-backed EC2.
  * A third container with STORAGE_BACKEND=memory shows why the in-memory build is not
    a valid target once writes are shared.
  * DynamoDB Local (same pinned image as CI) is the shared store.

Checks: /ready before and after seeding, /health vs /ready during a datastore outage,
shared state across processes, cross-process concurrency without overselling, restart
durability, a simulated rollback (all traffic to "ec2", then back), seed idempotency.

Safety: only Docker objects named legacy-api-rehearsal* are created and removed;
ports bind to 127.0.0.1; dummy credentials ("local") that DynamoDB Local accepts.
Exit 0 = every check passed, 1 = a check failed, 2 = environment problem.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PREFIX = "legacy-api-rehearsal"
NETWORK = PREFIX
DDB = f"{PREFIX}-ddb"
DDB_IMAGE = "amazon/dynamodb-local@sha256:ff89bd48ff32cd8d9be5fee8873b65b8854dc408f1afe881be6eb00247bc0dab"  # 3.3.1
TABLE = f"{PREFIX}-table"
APPS = {"ecs": 18081, "ec2": 18082, "memory": 18083}
DDB_ENV = {
    "STORAGE_BACKEND": "dynamodb",
    "DYNAMODB_TABLE_NAME": TABLE,
    "DYNAMODB_ENDPOINT_URL": f"http://{DDB}:8000",
    "AWS_REGION": "eu-west-2",
    "AWS_ACCESS_KEY_ID": "local",
    "AWS_SECRET_ACCESS_KEY": "local",  # DynamoDB Local dummy, never a real credential
    "WEB_CONCURRENCY": "2",
}
HARDENING = ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true"]

results = []


def docker(*args, check=True, capture=True):
    proc = subprocess.run(["docker", *args], capture_output=capture, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])} failed: {proc.stderr.strip()[:400]}")
    return proc.stdout.strip() if capture else ""


def http(method, port, path, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None, {}


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)
    return ok


def wait_for(port, path, want, seconds=60):
    status = None
    for _ in range(seconds * 2):
        status, _ = http("GET", port, path, timeout=3)
        if status == want:
            return status
        time.sleep(0.5)
    return status


def env_args(env):
    out = []
    for k, v in env.items():
        out += ["-e", f"{k}={v}"]
    return out


def start_app(name, image, env):
    docker("run", "-d", "--name", f"{PREFIX}-{name}", "--network", NETWORK,
           "-p", f"127.0.0.1:{APPS[name]}:5000", *env_args(env), *HARDENING, image)


def seed(image, create_table):
    args = ["python", "seed_dynamodb.py"] + (["--create-table"] if create_table else [])
    return docker("run", "--rm", "--network", NETWORK, *env_args(DDB_ENV), *HARDENING, image, *args)


def product(port, pid):
    _, body = http("GET", port, f"/api/v1/products/{pid}")
    return body.get("stock")


def orders(port):
    _, body = http("GET", port, "/api/v1/orders")
    return body.get("orders", [])


def cleanup():
    for name in list(APPS) + ["ddb"]:
        docker("rm", "-f", f"{PREFIX}-{name}", check=False)
    docker("network", "rm", NETWORK, check=False)


def rehearse(image):
    docker("network", "create", NETWORK)
    docker("run", "-d", "--name", DDB, "--network", NETWORK, DDB_IMAGE)
    time.sleep(3)

    # 1. Tasks started before the table exists/seeded must not be ready (ALB keeps them out)
    start_app("ecs", image, DDB_ENV)
    start_app("ec2", image, DDB_ENV)
    check("ecs: /health 200 (process alive) before the store exists", wait_for(APPS["ecs"], "/health", 200) == 200)
    check("ecs: /ready 503 before the table is created and seeded", http("GET", APPS["ecs"], "/ready")[0] == 503)

    # 2. One-off seed (the ECS seed task) creates the catalogue; re-running changes nothing
    seed(image, create_table=True)
    for name in ("ecs", "ec2"):
        check(f"{name}: /ready 200 after seeding", wait_for(APPS[name], "/ready", 200, 30) == 200)

    # 3. Shared state across independent processes
    status, order = http("POST", APPS["ecs"], "/api/v1/orders", {"product_id": 1, "quantity": 3})
    check("order placed via ecs (201)", status == 201, f"order id {order.get('id')}")
    check("ec2 sees the order written by ecs", any(o.get("id") == order.get("id") for o in orders(APPS["ec2"])))
    check("stock identical on both (100 - 3 = 97)", product(APPS["ecs"], 1) == product(APPS["ec2"], 1) == 97)
    status, order_b = http("POST", APPS["ec2"], "/api/v1/orders", {"product_id": 1, "quantity": 2})
    check("reverse direction: order via ec2 visible via ecs",
          status == 201 and any(o.get("id") == order_b.get("id") for o in orders(APPS["ecs"])))

    # 4. Concurrency across both processes (4 Gunicorn workers in total) cannot oversell
    stock_before = product(APPS["ecs"], 2)  # 50
    attempts = 80

    def buy(i):
        return http("POST", APPS["ecs" if i % 2 else "ec2"], "/api/v1/orders", {"product_id": 2, "quantity": 1})[0]

    with ThreadPoolExecutor(max_workers=32) as pool:
        codes = list(pool.map(buy, range(attempts)))
    ok, rejected, busy = codes.count(201), codes.count(400), codes.count(503)
    stock_after = product(APPS["ec2"], 2)
    check("concurrent orders: no oversell (stock never negative)", stock_after is not None and stock_after >= 0,
          f"{attempts} attempts: {ok} x 201, {rejected} x 400 insufficient, {busy} x 503 retry-later")
    check("concurrent orders: sold units == stock decrease", ok == stock_before - stock_after,
          f"stock {stock_before} -> {stock_after}")
    all_orders = orders(APPS["ecs"])
    ids = [o["id"] for o in all_orders]
    check("order ids unique across processes", len(ids) == len(set(ids)), f"{len(ids)} orders")
    _, stats = http("GET", APPS["ec2"], "/api/v1/stats")
    check("stats order count == orders list", stats.get("total_orders") == len(all_orders))

    # 5. Restart durability (the in-memory build would lose everything here)
    docker("restart", f"{PREFIX}-ecs")
    wait_for(APPS["ecs"], "/ready", 200, 60)
    check("ecs restarted: every order still present", len(orders(APPS["ecs"])) == len(all_orders))

    # 6. Datastore outage: liveness stays up, readiness goes down, then recovers.
    #    The emulator keeps data in memory, so the outage is a network cut, not a stop.
    docker("network", "disconnect", NETWORK, DDB)
    check("DynamoDB unreachable: /health still 200 (ECS does not kill the task)",
          http("GET", APPS["ecs"], "/health")[0] == 200)
    check("DynamoDB unreachable: /ready 503 (ALB stops routing)", http("GET", APPS["ecs"], "/ready", timeout=30)[0] == 503)
    docker("network", "connect", NETWORK, DDB)
    check("DynamoDB reachable again: /ready 200", wait_for(APPS["ecs"], "/ready", 200, 60) == 200)
    check("no data lost across the outage", len(orders(APPS["ecs"])) == len(all_orders))

    # 7. Simulated rollback: "ECS" removed, all traffic on the DynamoDB-backed "EC2"
    snapshot = (product(APPS["ecs"], 1), product(APPS["ecs"], 2), len(all_orders))
    docker("stop", f"{PREFIX}-ecs")
    check("rollback: ec2 serves the identical state",
          (product(APPS["ec2"], 1), product(APPS["ec2"], 2), len(orders(APPS["ec2"]))) == snapshot, str(snapshot))
    status, order_c = http("POST", APPS["ec2"], "/api/v1/orders", {"product_id": 3, "quantity": 1})
    check("rollback: writes continue on ec2", status == 201)
    docker("start", f"{PREFIX}-ecs")
    wait_for(APPS["ecs"], "/ready", 200, 60)
    check("roll forward: ecs sees the order written during rollback",
          any(o.get("id") == order_c.get("id") for o in orders(APPS["ecs"])))

    # 8. Seed is idempotent: re-running never resets stock or orders
    stock_1 = product(APPS["ec2"], 1)
    out = seed(image, create_table=False)
    check("re-seed leaves stock and orders unchanged",
          product(APPS["ec2"], 1) == stock_1 and len(orders(APPS["ec2"])) == len(all_orders) + 1, out[-120:])

    # 9. In-memory backend is NOT a valid migration target
    start_app("memory", image, {"STORAGE_BACKEND": "memory", "WEB_CONCURRENCY": "1"})
    wait_for(APPS["memory"], "/ready", 200, 60)
    check("memory backend: does not see shared orders (separate private state)",
          len(orders(APPS["memory"])) == 0 and product(APPS["memory"], 1) == 100)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--image", default="legacy-api:rehearsal")
    ap.add_argument("--skip-build", action="store_true", help="use an existing --image")
    args = ap.parse_args(argv)
    try:
        docker("info", "--format", "{{.ServerVersion}}")
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"Docker is not available: {exc}", file=sys.stderr)
        return 2
    cleanup()  # only our own leftovers
    try:
        if not args.skip_build:
            print(f"Building {args.image} from the repository Dockerfile ...", flush=True)
            docker("build", "-t", args.image, ".", capture=False)
        rehearse(args.image)
    except RuntimeError as exc:
        check("rehearsal ran to completion", False, str(exc))
    finally:
        for name in APPS:
            logs = docker("logs", "--tail", "3", f"{PREFIX}-{name}", check=False)
            if logs:
                print(f"--- last log lines of {name}:\n{logs}")
        cleanup()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed (LOCAL rehearsal; not AWS, not an ALB)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
