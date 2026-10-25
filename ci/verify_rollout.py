#!/usr/bin/env python3
"""Verify a deployment after `terraform apply` and print diagnostics on failure.

    terraform output -json > outputs.json
    python ci/verify_rollout.py --outputs outputs.json --region "$AWS_REGION"

Checks (all must pass):
  1. ECS service (if it exists) is stable: one deployment, rolloutState COMPLETED,
     runningCount == desiredCount   (`aws ecs wait services-stable` first)
  2. ALB target health: legacy EC2 target healthy (it is the rollback target);
     ECS targets healthy and at least desiredCount of them
  3. HTTP 200 from <alb_url>/health and <alb_url>/ready

Uses the AWS CLI (credentials from the workflow's OIDC role). Exit 0 = verified,
1 = verification failed (diagnostics printed), 2 = usage/input error.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request


def aws_cli(args, region, timeout=900):
    """Run an AWS CLI command and return parsed JSON (None for empty output)."""
    proc = subprocess.run(["aws", *args, "--region", region, "--output", "json"],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:2])} failed: {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def http_status(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return None


class Verifier:
    def __init__(self, outputs, region, aws=aws_cli, http=http_status, sleep=time.sleep,
                 http_attempts=10, http_interval=6):
        self.o, self.region = outputs, region
        self.aws, self.http, self.sleep = aws, http, sleep
        self.http_attempts, self.http_interval = http_attempts, http_interval
        self.failures, self.desired = [], None

    def call(self, *args, **kw):
        return self.aws(list(args), self.region, **kw)

    def check_service(self):
        cluster, service = self.o.get("ecs_cluster_name"), self.o.get("ecs_service_name")
        if not service:
            print("ECS service not enabled yet - skipping service checks")
            return
        try:
            self.call("ecs", "wait", "services-stable", "--cluster", cluster, "--services", service)
        except Exception as exc:  # noqa: BLE001 - report and continue to diagnostics
            self.failures.append(f"ECS service did not become stable: {exc}")
        svc = self.call("ecs", "describe-services", "--cluster", cluster, "--services", service)["services"][0]
        deployments = svc.get("deployments", [])
        primary = next((d for d in deployments if d.get("status") == "PRIMARY"), {})
        self.desired = svc.get("desiredCount", 0)
        print(f"ECS {service}: desired={self.desired} running={svc.get('runningCount')} "
              f"deployments={len(deployments)} rollout={primary.get('rolloutState')}")
        if len(deployments) != 1 or primary.get("rolloutState") != "COMPLETED":
            self.failures.append(f"ECS rollout not completed (deployments={len(deployments)}, "
                                 f"rolloutState={primary.get('rolloutState')})")
        if svc.get("runningCount") != self.desired:
            self.failures.append(f"ECS running {svc.get('runningCount')} != desired {self.desired}")

    def target_states(self, tg_arn):
        health = self.call("elbv2", "describe-target-health", "--target-group-arn", tg_arn)
        return [d["TargetHealth"]["State"] for d in health.get("TargetHealthDescriptions", [])]

    def wait_in_service(self, tg_arn, label, attempts):
        """Wait for targets to pass health checks (a new EC2 instance needs several
        minutes of bootstrap first). Each AWS waiter attempt polls for ~10 minutes."""
        for attempt in range(1, attempts + 1):
            try:
                self.call("elbv2", "wait", "target-in-service", "--target-group-arn", tg_arn)
                return
            except Exception as exc:  # noqa: BLE001 - the state check below reports the failure
                print(f"{label}: targets not in service yet (wait {attempt}/{attempts}): {exc}")

    def check_targets(self):
        self.wait_in_service(self.o["ec2_target_group_arn"], "EC2", attempts=2)
        ec2 = self.target_states(self.o["ec2_target_group_arn"])
        print(f"EC2 target group: {ec2}")
        if not ec2 or any(s != "healthy" for s in ec2):
            self.failures.append(f"legacy EC2 (rollback target) not healthy: {ec2}")
        if self.o.get("ecs_service_name"):
            self.wait_in_service(self.o["ecs_target_group_arn"], "ECS", attempts=1)
            ecs = self.target_states(self.o["ecs_target_group_arn"])
            healthy = ecs.count("healthy")
            print(f"ECS target group: {ecs}")
            if healthy < (self.desired or 1) or any(s == "unhealthy" for s in ecs):
                self.failures.append(f"ECS targets not healthy: {ecs} (desired {self.desired})")

    def check_http(self):
        base = self.o["alb_url"].rstrip("/")
        for path in ("/health", "/ready"):
            status = None
            for attempt in range(self.http_attempts):
                status = self.http(base + path)
                if status == 200:
                    break
                self.sleep(self.http_interval)
            print(f"GET {path} via ALB -> {status}")
            if status != 200:
                self.failures.append(f"GET {path} via ALB returned {status}")

    def diagnostics(self):
        print("\n=== Diagnostics ===")
        cluster, service = self.o.get("ecs_cluster_name"), self.o.get("ecs_service_name")
        try:
            if service:
                svc = self.call("ecs", "describe-services", "--cluster", cluster, "--services", service)["services"][0]
                print("Recent ECS service events:")
                for ev in svc.get("events", [])[:10]:
                    print(f"  {ev.get('createdAt')} {ev.get('message')}")
                stopped = self.call("ecs", "list-tasks", "--cluster", cluster, "--service-name", service,
                                    "--desired-status", "STOPPED").get("taskArns", [])[:5]
                if stopped:
                    tasks = self.call("ecs", "describe-tasks", "--cluster", cluster, "--tasks", *stopped)
                    for t in tasks.get("tasks", []):
                        reasons = [c.get("reason") for c in t.get("containers", []) if c.get("reason")]
                        print(f"  stopped task: {t.get('stoppedReason')} {reasons}")
            for key in ("ec2_target_group_arn", "ecs_target_group_arn"):
                health = self.call("elbv2", "describe-target-health", "--target-group-arn", self.o[key])
                for d in health.get("TargetHealthDescriptions", []):
                    th = d["TargetHealth"]
                    print(f"  {key.split('_')[0]} target {d['Target'].get('Id')}: {th.get('State')} "
                          f"{th.get('Reason', '')} {th.get('Description', '')}")
        except Exception as exc:  # noqa: BLE001 - diagnostics must never hide the real failure
            print(f"  (diagnostics incomplete: {exc})")

    def run(self):
        self.check_service()
        self.check_targets()
        self.check_http()
        if self.failures:
            print("\nVERIFICATION FAILED:")
            for f in self.failures:
                print(f"  - {f}")
            self.diagnostics()
            return 1
        print("\nDeployment verified.")
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--outputs", required=True, help="`terraform output -json` file")
    ap.add_argument("--region", required=True)
    args = ap.parse_args(argv)
    try:
        with open(args.outputs, encoding="utf-8") as fh:
            outputs = {k: v.get("value") for k, v in json.load(fh).items()}
        for key in ("alb_url", "ec2_target_group_arn", "ecs_target_group_arn", "ecs_cluster_name"):
            if not outputs.get(key):
                raise ValueError(f"missing Terraform output {key}")
    except (OSError, ValueError, AttributeError) as exc:
        print(f"verify_rollout: {exc}", file=sys.stderr)
        return 2
    return Verifier(outputs, args.region).run()


if __name__ == "__main__":
    sys.exit(main())
