# Public release and deployment approval (Task 14 assessment)

Status on 2026-10-26: the repository is **private**. Nothing below has been configured.
Decision recorded: public repository + protected GitHub Environment `production`,
**conditional on CoderCo permission** (section 1).

## 1. CoderCo redistribution permission: BLOCKED

| Source | What it says |
|---|---|
| [codercohq/ec2-legacy-app](https://github.com/codercohq/ec2-legacy-app) (public, 2 commits) | No LICENSE/COPYING file; GitHub detects no licence (`license: null`, community profile `license=false`) |
| [README "License" section](https://github.com/codercohq/ec2-legacy-app/blob/main/README.md#license) | "This is a learning project for CoderCo Community." No grant of rights |
| CoderCo org: other repos | Some are MIT (e.g. `terraform-starter-kit`, `eks-assignment-v1`); that licence does not apply to this repository |
| [codercohq/ecs-assignment](https://github.com/codercohq/ecs-assignment), [codercohq/challenges](https://github.com/codercohq/challenges) | Other assignments say "Create your own repository" / "Submit a GitHub repo"; no licence, no statement about this repository or public redistribution |
| [GitHub: licensing a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository) | Without a licence "the default copyright laws apply ... no one may reproduce, distribute, or create derivative works" |
| [GitHub Terms, D.5](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service) | Public repos let others view and **fork** through GitHub. This repository is a standalone copy (`fork: false`), not a GitHub fork |

Conclusion: public redistribution of the starter code is **not established**. Get written
permission from CoderCo (e.g. the course owner) that students may publish solutions
containing the starter code publicly, and keep that message. Then add the attribution in
section 5. Do not add a licence covering CoderCo's files.

## 2. Exposure audit (all history: 3 commits, 136 paths, `main` only)

| Class | Finding | Action |
|---|---|---|
| Secrets | None. gitleaks v8.30.1 over all history: no leaks. Without the repository allowlist: 3 hits, all the reviewed public CPython `GPG_KEY` fingerprint. No AWS keys, tokens or private keys in any blob | None |
| Identifying, low | Accidental file `ersFeras_FDesktopEC2ec2-legacy-app-main` + U+F022 (commit `9bcd827`): content is 15 lines of `git diff --check` whitespace output; the **name** contains the local Windows user name and path | Remove with a normal commit after approval; it stays in history (low sensitivity, no rewrite recommended) |
| Identifying, low | AWS account ID **last 4 digits** in 3 evidence summaries (task 10-12); full ID appears nowhere | Accept (partial, non-secret) or redact in a new commit; history keeps it |
| Operational, low | `evidence/task-10-bootstrap-plan/01-summary.txt` describes the account: OIDC provider date, "7 roles from other projects" (no names), IAM role count, quotas, otherwise-empty region | Accept or trim in a new commit |
| Normal identifiers | GitHub owner/repo numeric IDs (public via API), role and bucket name patterns, IAM policies, plan text with `<ACCOUNT_ID>` | Normal for public IaC |
| Harmless | `111122223333` (AWS example account), `203.0.113.10` (documentation IP range), `you@example.com`, `AWS_SECRET_ACCESS_KEY=local` (DynamoDB Local dummy), private CIDRs | None |
| Commit metadata | All commits authored/committed as `Fer-as <78626961+Fer-as@users.noreply.github.com>`; no co-author trailers | None |

Future exposure once public: Actions logs are public. `deploy.yml` apply output and
role ARNs would show the full account ID (`configure-aws-credentials` does not mask it by
default). Optional: set `mask-aws-account-id: true` on the three credential steps.

## 3. Protected `production` environment (configure only after section 1 is cleared)

Facts ([deployments and environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments),
[manage environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments),
[review deployments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments)):
on GitHub Free, environments with required reviewers exist only for **public**
repositories; up to 6 reviewers, one approval is enough; a job cannot use environment
secrets until approved; "Reject" fails the workflow; **a workflow that references a
missing environment creates it without protection rules**.

**Order matters:** configure and verify `production` *before* setting the deploy
repository variables and before the bootstrap apply. Until then `deploy.yml` fails in
preflight (variables missing) and the AWS roles do not exist.

Settings → Environments → New environment → `production`:

| Setting | Value |
|---|---|
| Required reviewers | Yes: you (and an independent reviewer if available) |
| Prevent self-review | **Off** if you are the only reviewer (otherwise every run deadlocks). **On** only when a second reviewer exists who does not trigger deployments |
| Allow administrators to bypass configured protection rules | **Off** (clear it) |
| Deployment branches and tags | Selected branches and tags → Branch rule `main` |
| Wait timer | 0 |
| Environment secrets | None needed (the workflow uses OIDC) |

Only reviewer = you: approval is a deliberate **operator checkpoint**, recorded by
GitHub, not an independent review. Independent reviewer: add them (they need at least
read access), turn on Prevent self-review, and never let them trigger deployments, so
there is always someone who may approve.

Also for a public repository (Settings → Actions → General): workflow permissions stay
"Read repository contents" (already `read`); fork pull request workflows: require
approval for outside contributors. Settings → Rules: a ruleset for `main` that blocks
force pushes and deletion and requires the `CI gate` and `Terraform gate` checks.
Settings → Code security: secret scanning and push protection (free on public repos).

Read-only verification (PowerShell):

```powershell
gh api repos/Fer-as/ec2-to-ecs-fargate-migration/environments/production `
  --jq '{protection_rules, deployment_branch_policy, can_admins_bypass}'
gh api repos/Fer-as/ec2-to-ecs-fargate-migration/environments/production/deployment-branch-policies `
  --jq '.branch_policies[] | {name, type}'
```

Expected: a `required_reviewers` rule listing the reviewer(s) and the intended
`prevent_self_review`; `deployment_branch_policy` with `custom_branch_policies: true`;
one policy `main` of type `branch`; `can_admins_bypass: false` (if the field is not
returned, confirm the checkbox in the UI).

## 4. Approval-gate test (no AWS access; run before the bootstrap apply)

> **Superseded (Task 16):** the implemented test is
> [.github/workflows/approval-gate-test.yml](../.github/workflows/approval-gate-test.yml)
> with the procedure in [approval-gate-test.md](approval-gate-test.md). It requests **no**
> OIDC token at all (simpler and safer than the draft below), and
> `ci/tests/test_workflow_privileges.py` now checks every workflow file. The draft is
> kept for the record.

Add temporarily as `.github/workflows/approval-gate-test.yml` (commit on `main`, run,
then delete in a later commit). It has no AWS steps; before the bootstrap apply no AWS
role exists, so the tokens it requests cannot be used anywhere.

```yaml
name: Approval gate test (no AWS)
on:
  workflow_dispatch:
permissions: {}
jobs:
  unprotected:
    name: Unprotected job (prints OIDC subject, never the token)
    runs-on: ubuntu-24.04
    permissions:
      id-token: write
    steps:
      - &sub
        name: OIDC subject
        run: |
          python3 - <<'EOF'
          import base64, json, os, urllib.request
          req = urllib.request.Request(os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=sts.amazonaws.com",
                                       headers={"Authorization": "bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
          payload = json.load(urllib.request.urlopen(req))["value"].split(".")[1]
          print("sub:", json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["sub"])
          EOF
  gated:
    name: Waits for production approval
    needs: unprotected
    runs-on: ubuntu-24.04
    environment: production
    permissions:
      id-token: write
    steps:
      - run: echo "Approved; job started at $(date -u +%FT%TZ)"
      - *sub
```

(Checked with the pinned actionlint 1.7.12: no findings.)

| # | Test | Expected result | Evidence |
|---|---|---|---|
| 1 | Run on `main` | `unprotected` succeeds, `sub` ends `:ref:refs/heads/main`; `gated` shows **Waiting** for review | screenshot + run URL |
| 2 | Click **Reject** | `gated` never starts; run fails | screenshot |
| 3 | Run again, **Approve and deploy** | `gated` starts; `sub` ends `:environment:production` | screenshot + log line |
| 4 | Run from another branch | `gated` is rejected by the branch rule (never waits for approval) | screenshot |
| 5 | Unprotected path | Only `environment:production` tokens (issued after approval) match the apply role trust; the plan and image-push roles trust only `ref:refs/heads/main` and have no apply rights ([iam-permission-matrix.md](iam-permission-matrix.md)) | logs from 1 and 3 + bootstrap plan trust policies |

The environment counts as **verified** only after tests 1-4 pass.
`ci/tests/test_workflow_policy.py` checks OIDC use only in `ci.yml`, `terraform-ci.yml`
and `deploy.yml`; extend it to fail on any other workflow requesting `id-token`
(allow-listing the test file while it exists).

## 5. Attribution to add before publishing

The README already credits CoderCo and lists the migration work. Add: the upstream link
`https://github.com/codercohq/ec2-legacy-app`, the date or commit it was copied from, a
statement that the starter code remains CoderCo's and is published with their permission
(quote or reference it), and that no licence is granted for it. Make clear the repository
is a portfolio migration built from that starter.
