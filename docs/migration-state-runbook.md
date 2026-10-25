# Migration state runbook: shared DynamoDB storage

How orders and stock are kept correct while traffic moves from the legacy EC2
instance to ECS Fargate, and how to roll back without losing data.

## 1. The problem

The original app keeps products, stock and the order counter in Python process memory.

- Each Gunicorn worker has its own copy. Proven by `tests/test_gunicorn_workers.py`
  (memory backend): two workers both issued order ID 1, and after one worker sold
  5 units the other still reported stock 100.
- `--max-requests 1000` on EC2 restarts each worker roughly every 1,000 requests,
  silently wiping that worker's data. Any restart, deploy or scale event does the same.
- EC2 and ECS running side by side would be two more independent copies.

In-memory data is therefore not durable, not consistent and cannot be trusted as a
source of truth. It must never be used for writable traffic on more than one process.

## 2. The fix: `STORAGE_BACKEND=dynamodb`

One DynamoDB table shared by every process (EC2 workers, ECS tasks).

| Item | pk | sk | Attributes |
|---|---|---|---|
| Product | `PRODUCT` | product id | name, price (exact decimal), stock |
| Order | `ORDER` | order id | product_id, product_name, quantity, total_price (decimal), created_at |
| Order sequence | `SEQUENCE` | 0 | last_order_id |

Guarantees (tested against DynamoDB Local, `tests/test_dynamodb_integration.py`):

- **Unique integer order IDs:** atomic `ADD` on the sequence item.
- **No overselling:** stock is decremented with the condition `stock >= quantity`.
- **All or nothing:** the stock decrement and the order `Put` are one `TransactWriteItems`.
  The order `Put` also requires `attribute_not_exists`, so an existing order is never overwritten.
- **Contention:** transaction conflicts are retried up to 5 times with jittered backoff,
  then the API returns `503` with no partial state.
- **Consistent reads:** strongly consistent `GetItem`/`Query`, with pagination followed.
- **Money:** stored as exact decimals; the API still returns JSON numbers.
- **No reseeding at start-up:** the app never writes initial data.

## 3. Configuration

| Variable | Value | Notes |
|---|---|---|
| `STORAGE_BACKEND` | `dynamodb` | default `memory` (legacy behaviour, one worker only) |
| `DYNAMODB_TABLE_NAME` | table name | required with `dynamodb` |
| `AWS_REGION` | e.g. `eu-west-2` | required with `dynamodb` (or `AWS_DEFAULT_REGION`) |
| `DYNAMODB_ENDPOINT_URL` | unset in AWS | DynamoDB Local only |
| `WEB_CONCURRENCY` | Gunicorn workers | keep `1` with `memory`; may be raised with `dynamodb` |

Credentials come from the AWS SDK default chain: the ECS task role, or the EC2 instance
profile. No keys in images, task definitions or env files.

- **ECS:** set the variables in the task definition.
- **EC2:** write them to `/etc/flask-app/flask-app.env`; the systemd unit already loads
  that file if it exists.

## 4. IAM (least privilege, for Terraform)

Implemented in `terraform/iam.tf`, further narrowed per item type with `dynamodb:LeadingKeys`
(see [aws-platform.md](aws-platform.md) section 4).

Scope every statement to the table ARN only (no `*`, no indexes are used).

| Principal | Actions |
|---|---|
| App: ECS task role and EC2 instance profile | `dynamodb:GetItem`, `dynamodb:Query`, `dynamodb:PutItem`, `dynamodb:UpdateItem` |
| Seed (one-off ECS task or operator) | `dynamodb:PutItem` (plus `GetItem` for verification) |

`TransactWriteItems` is authorised per item, by the `UpdateItem` and `PutItem`
permissions above. The app needs no `Scan`, `DeleteItem`, `CreateTable` or
`DescribeTable`. The ECS task *execution* role (image pull, logs) is separate and gets
no DynamoDB access.

Table settings for Terraform:
- on-demand billing
- partition key `pk` (String), sort key `sk` (Number)
- point-in-time recovery on
- deletion protection on
- default encryption

## 5. Initialising the shared store

Terraform steps: [aws-platform.md](aws-platform.md) section 6, step 4 (one-off seed ECS task).

1. Create the table with Terraform.
2. Run the seed **once**, as a one-off ECS task with the same image or from the EC2 host:
   `python seed_dynamodb.py` (with `DYNAMODB_TABLE_NAME` and `AWS_REGION` set).
   It only creates missing items and never changes existing stock or the sequence.
   Re-running it is safe.
3. Check `GET /ready` returns `200` on every target. It returns `503` until the store is seeded.

## 6. Existing legacy orders and stock

No reliable export of the legacy in-memory data exists. Each of the 4 workers holds a
different partial copy, `GET /api/v1/orders` answers from whichever worker receives the
request, and order IDs collide between workers. Any "export" would be incomplete and
contain duplicate IDs.

Decision for this project:
- Declare a **controlled write freeze**.
- Accept that in-memory legacy orders are discarded.
- Start the shared store from the **fresh seed dataset** (the original catalogue and stock).

If real business data were involved, the cutover would instead need:
1. a write freeze
2. a supervised reconciliation of stock and orders from an authoritative source outside the app
3. a seed script fed by that reconciled data

## 7. Cutover sequence

1. **Prepare:** Terraform creates the table and IAM, and the seed runs. ECS is deployed with
   `STORAGE_BACKEND=dynamodb` but receives 0% of traffic.
2. **Write freeze:** announce the freeze, then pause or stop POST traffic.
3. **Switch EC2 to the shared store:** set `legacy_storage_backend = "dynamodb"` and apply.
   Terraform replaces the instance, and its user data writes `/etc/flask-app/flask-app.env`. EC2 is now the
   **DynamoDB-backed rollback target**.
4. **Verify (section 9)**, then lift the write freeze.
5. **Shift traffic** with ALB weights: EC2/ECS 100/0 → 90/10 → 50/50 → 0/100, checking
   alarms and the checks in section 9 at each step.
6. Keep EC2 running and registered at weight 0 until the agreed rollback window ends.

**Rollback:** set the ALB weights back to EC2. Both sides read and write the same table, so
no orders or stock are lost and nothing needs copying.

## 8. Why the old in-memory version is not a rollback target

Once shared writes begin, DynamoDB holds the truth. Rolling back to the in-memory build
would mean:
- serving the original seed stock
- losing every order written since cutover
- restarting order IDs at 1, producing duplicates of IDs already issued

Only an EC2 instance running the **same DynamoDB-backed version** is a valid rollback target.

## 9. Consistency checks before (and during) traffic shifting

- `GET /ready` returns `200` on EC2 and on every ECS task.
- `GET /api/v1/products`, `/api/v1/orders` and `/api/v1/stats` return identical
  results via EC2 and via ECS.
- Write test: place one order via ECS and confirm EC2 shows it and the stock change,
  then the reverse.
- The order count in `/api/v1/stats` equals the order count in `/api/v1/orders`, and no
  stock is negative.
- No `503` contention responses or `Readiness check failed` errors in the logs.

## 10. Conditions that block cutover

- Any target still on `STORAGE_BACKEND=memory` while receiving writable traffic.
- `/ready` not `200` on every target, or seed not confirmed.
- EC2 and ECS showing different orders or stock.
- The EC2 rollback target not on the DynamoDB-backed version.
- Point-in-time recovery or deletion protection not enabled on the table.
- IAM wider than section 4, or static credentials anywhere.
- No alarms on ALB 5xx and unhealthy targets.

## 11. Known limitations

- **Request idempotency:** if a client retries a `POST` after a timeout, a second order
  can be created. The ALB does not retry requests, and the AWS SDK's own transaction
  retries are idempotent (client request token), but there is no `Idempotency-Key`
  support in the API.
- **ID gaps:** an ID is used up when a transaction is cancelled (stock race, conflict),
  so IDs are unique and increasing but not always consecutive.
- **Single partitions:** all orders share one partition key, and so do all products. That's
  fine at this scale (about 1,000 writes/s per partition), but not for high volume.
- **Full reads:** `/api/v1/orders` and `/api/v1/stats` read every order, so cost grows
  with the order count.
- **Price changes:** the order uses the price read just before the transaction. Prices
  never change in this app.
- **DynamoDB Local differences:** it serialises transactions and never returns
  `TransactionConflict`, so the retry path is covered by an injected-failure test.
  Real-AWS behaviour still needs a check after deployment.
- **Readiness:** returns `503` within about 3 s when DynamoDB is unresponsive. DNS
  failures can take longer.
