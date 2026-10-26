# Approval gate test (production environment)

Workflow: [.github/workflows/approval-gate-test.yml](../.github/workflows/approval-gate-test.yml).
Manual trigger only, `contents: read`, **no OIDC token, no AWS, no Terraform, no secrets,
no actions**. One job, `approval`, uses `environment: production`; its only real step
prints a message. `ci/tests/test_workflow_privileges.py` fails if any of that changes.

| State | Meaning | Status (2026-10-26) |
|---|---|---|
| **Configured** | `production` exists with required reviewer `Fer-as`, prevent self-review off, admin bypass off, deployment branches `main` only (read back with the GitHub API) | Done (by the owner) |
| **Runtime verified** | Tests 1-3 below passed on GitHub and the evidence is saved | **Not yet** |

Only "runtime verified" proves the gate blocks a job. With one reviewer who also
triggers runs, approval is an **operator checkpoint**, not an independent review.

## Before the tests

1. Commit and push the workflow and its tests to `main` yourself.
2. Wait for `CI` and `Terraform CI` on that commit to pass.
3. Read the configuration back (keep the output as evidence):

```powershell
gh api repos/Fer-as/ec2-to-ecs-fargate-migration/environments/production `
  --jq '{name, can_admins_bypass, protection_rules, deployment_branch_policy}'
gh api repos/Fer-as/ec2-to-ecs-fargate-migration/environments/production/deployment-branch-policies `
  --jq '.branch_policies[] | {name, type}'
```

## Test 1: rejection blocks the job

1. Actions → *Approval gate test (harmless, no AWS)* → Run workflow → branch `main`.
2. Open the run. **Expected:** job *Waits for production approval…* shows
   "Waiting" / "Review deployments", and no step has run. Screenshot.
3. Review deployments → tick `production` → comment "rejection test" → **Reject**.
4. **Expected:** the run ends as failed; the job's steps never ran (no "APPROVAL GATE
   TEST" line in any log). Screenshot of the summary and the job.
5. STOP if the job started without a review request: the environment is not
   protecting jobs. Do not set any deploy variables or apply the bootstrap.

## Test 2: approval lets the job run

1. Run the workflow again on `main`. **Expected:** waiting for review, as in Test 1.
2. Review deployments → `production` → comment "approval test" → **Approve and deploy**.
3. **Expected:** the job runs; the log shows
   `APPROVAL GATE TEST: this step ran after a required reviewer approved the job.`
   with run id, `ref=refs/heads/main`, SHA and time. Screenshots: review dialog,
   successful run, log line. Environments page shows the deployment and approver.

## Test 3: a non-main branch cannot use production

No setting changes; a throwaway branch only.

1. `git switch -c approval-branch-probe` (from `main`), `git push -u origin approval-branch-probe`.
2. Actions → *Approval gate test* → Run workflow → branch **approval-branch-probe**.
3. **Expected:** the job fails at once with a message like *"Branch
   approval-branch-probe is not allowed to deploy to production due to environment
   protection rules"*; no review request; no step runs. Screenshot.
4. If instead it waits for review, the branch rule is not working: **Reject**, record
   it, and fix the environment before going further. (Even if approved, the job's
   first step exits 1 on a non-main ref, so nothing else would run.)
5. Delete the branch: `git push origin --delete approval-branch-probe`,
   `git switch main`, `git branch -D approval-branch-probe`.

## Evidence to keep

Save screenshots plus run URLs in `evidence/` (e.g. `task-17-approval-gate/`):
config read-back, Test 1 waiting and rejected, Test 2 waiting, approved and log
line, Test 3 refusal. Then mark "GitHub protected deployment" **Verified (GitHub)** in
[acceptance-checklist.md](acceptance-checklist.md).

## Why this cannot reach AWS

- No `id-token: write`, so GitHub issues no OIDC token to this workflow; the AWS
  roles accept only OIDC tokens with exact subjects.
- Only `deploy.yml` jobs `publish`, `plan` and `apply` may request tokens
  (`test_workflow_privileges.py` checks every workflow file, and 21 unsafe variants
  plus a real unsafe file are rejected:
  [evidence](../evidence/task-16-predeployment/02-workflow-policy-negative-tests.txt)).
- The apply role trusts only `...:environment:production`; this workflow never
  receives any token, approved or not.
