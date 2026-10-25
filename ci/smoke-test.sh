#!/bin/bash
# Smoke test for a running legacy-api container.
# Usage: ci/smoke-test.sh [base-url]   (default http://127.0.0.1:5000)
# Used by .github/workflows/ci.yml; expects a FRESH container (first order gets id 1).
# Exits non-zero if any check fails. Requires curl and python3 (for JSON checks).
set -uo pipefail

BASE_URL="${1:-http://127.0.0.1:5000}"
read -ra PY <<<"${PYTHON:-python3}"   # e.g. PYTHON="py -3.12" on Windows
FAILURES=0

# check <name> <expected-status> <python-expression on `b` (parsed JSON body)> <curl args...>
check() {
    local name=$1 expected=$2 expr=$3; shift 3
    local out status body
    out=$(curl -s -w '\n%{http_code}' "$@")
    status=$(tail -n1 <<<"$out")
    body=$(sed '$d' <<<"$out")
    if [ "$status" = "$expected" ] && "${PY[@]}" -c "import json,sys; b=json.loads(sys.argv[1]); sys.exit(0 if ($expr) else 1)" "$body" 2>/dev/null; then
        echo "PASS  $name -> HTTP $status $body"
    else
        echo "FAIL  $name -> expected HTTP $expected, got HTTP $status $body"
        FAILURES=$((FAILURES + 1))
    fi
}

J=(-H "Content-Type: application/json")

echo "Smoke test against $BASE_URL"
check "GET /health" 200 'b["status"]=="healthy" and b["service"]=="legacy-api" and b["version"]=="1.0.0" and b["environment"]=="production"' "$BASE_URL/health"
check "GET /api/v1/products" 200 'b["count"]==3 and [p["id"] for p in b["products"]]==[1,2,3]' "$BASE_URL/api/v1/products"
check "GET /api/v1/products/1" 200 'b=={"id":1,"name":"Widget A","price":29.99,"stock":100}' "$BASE_URL/api/v1/products/1"
check "GET /api/v1/products/999" 404 'b=={"error":"Product not found"}' "$BASE_URL/api/v1/products/999"
check "GET /api/v1/orders (empty)" 200 'b=={"orders":[],"count":0}' "$BASE_URL/api/v1/orders"
check "POST /api/v1/orders (valid)" 201 'b["id"]==1 and b["product_id"]==1 and b["quantity"]==2 and b["total_price"]==59.98 and b["product_name"]=="Widget A"' -X POST "${J[@]}" -d '{"product_id":1,"quantity":2}' "$BASE_URL/api/v1/orders"
check "GET /api/v1/orders (has order)" 200 'b["count"]==1 and b["orders"][0]["id"]==1 and b["orders"][0]["quantity"]==2' "$BASE_URL/api/v1/orders"
check "GET /api/v1/products/1 (stock 98)" 200 'b["stock"]==98' "$BASE_URL/api/v1/products/1"
check "GET /api/v1/stats" 200 'b["total_products"]==3 and b["total_orders"]==1 and b["total_revenue"]==59.98' "$BASE_URL/api/v1/stats"
check "POST quantity -1" 400 'b=={"error":"400 Bad Request: quantity must be a positive integer"}' -X POST "${J[@]}" -d '{"product_id":1,"quantity":-1}' "$BASE_URL/api/v1/orders"
check "POST quantity 0" 400 'b=={"error":"400 Bad Request: quantity must be a positive integer"}' -X POST "${J[@]}" -d '{"product_id":1,"quantity":0}' "$BASE_URL/api/v1/orders"
check "POST quantity \"abc\"" 400 'b=={"error":"400 Bad Request: quantity must be a positive integer"}' -X POST "${J[@]}" -d '{"product_id":1,"quantity":"abc"}' "$BASE_URL/api/v1/orders"
check "POST missing product_id" 400 'b=={"error":"400 Bad Request: product_id is required"}' -X POST "${J[@]}" -d '{"quantity":1}' "$BASE_URL/api/v1/orders"
check "GET /does-not-exist" 404 'b=={"error":"Not found"}' "$BASE_URL/does-not-exist"
check "GET /" 404 'b=={"error":"Not found"}' "$BASE_URL/"
check "DELETE /api/v1/products/1" 405 'b=={"error":"Method not allowed"}' -X DELETE "$BASE_URL/api/v1/products/1"
check "GET /api/v1/stats (invalid orders not counted)" 200 'b["total_orders"]==1' "$BASE_URL/api/v1/stats"

echo "Failures: $FAILURES"
exit "$FAILURES"
