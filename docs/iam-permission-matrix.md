# Pipeline IAM permission matrix (Tasks 11-12)

Policies: [terraform/bootstrap/policies/](../terraform/bootstrap/policies/), rendered by
[github_oidc.tf](../terraform/bootstrap/github_oidc.tf) with ARNs built from names, so
`terraform plan` shows them in full. Sources: the AWS Service Authorization Reference
(machine-readable service reference JSON for ec2, ecs, elasticloadbalancing,
application-autoscaling, logs, sns, cloudwatch, iam, read on 2026-10-25) and the ECS
"IAM permissions required for service auto scaling" page.

Notation: `P` = `legacy-api` (project), `R` = `eu-west-2`, `A` = account ID.
**RequestTag** = `aws:RequestTag/Project = P` required on create.
**ResourceTag** = `aws:ResourceTag/Project = P` required on the existing resource.
**Name** = ARN pattern restricted to `P-*` names.

## 1. Roles

| Role | Trust (`sub`, exact) | Policies |
|---|---|---|
| `gha-legacy-api-plan` | `...:ref:refs/heads/main` | platform-read, plan-store-write |
| `gha-legacy-api-image-push` | `...:ref:refs/heads/main` | image-push |
| `gha-legacy-api-apply` | `...:environment:production` (unchanged, see section 6) | inline: platform-read, plan-store-read; customer-managed: `gha-legacy-api-apply-network`, `-apply-services`, `-apply-core` |

**Size quotas (Task 12).** IAM caps a role's inline policies at 10,240 characters in total
and each managed policy at 6,144 (whitespace not counted;
[IAM quotas](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html)).
Task 11's single inline write policy (13,734) plus the read policies came to 15,667
characters, so the bootstrap apply would have failed. Rendered sizes now: apply inline
1,933; apply managed 5,335 / 5,580 / 2,936; plan inline 1,934; image-push 450.
`ci/tests/test_rendered_policy_gate.py` and `terraform test` enforce both limits.

## 2. Apply role: Terraform resource → API permissions → restriction

| Platform resources | Create | Read (plan/refresh) | Update | Delete | How it is restricted |
|---|---|---|---|---|---|
| `aws_vpc` | CreateVpc | ec2:Describe* (`*`) | ModifyVpcAttribute | DeleteVpc | RequestTag on create; ResourceTag after |
| `aws_subnet` (x4) | CreateSubnet (subnet + vpc) | Describe* | ModifySubnetAttribute | DeleteSubnet | subnet: RequestTag; parent vpc: ResourceTag |
| `aws_internet_gateway` | CreateInternetGateway, Attach (igw + vpc) | Describe* | — | Detach, DeleteInternetGateway | RequestTag; ResourceTag on igw and vpc |
| `aws_route_table`, routes, associations | CreateRouteTable, CreateRoute, AssociateRouteTable | Describe* | ReplaceRoute | DeleteRoute, Disassociate, DeleteRouteTable | RequestTag; ResourceTag on route-table, subnet, vpc, igw |
| `aws_security_group`, `aws_vpc_security_group_*_rule` | CreateSecurityGroup, Authorize* | Describe* | ModifySecurityGroupRules, UpdateSecurityGroupRuleDescriptions* | Revoke*, DeleteSecurityGroup | SG: RequestTag/ResourceTag; `security-group-rule` child: no tag condition (only reachable through a project SG) |
| VPC default security group | **Not managed (Task 12)** | — | — | — | No permission on untagged security groups. See section 5 |
| `aws_eip` (x2) | AllocateAddress | Describe* | AssociateAddress, DisassociateAddress | ReleaseAddress | RequestTag; ResourceTag on eip, instance; ENI child unconditioned. **Released association (Task 33):** DisassociateAddress on the literal resource `arn:aws:ec2:<region>:<account>:*/*` only (see section 4) |
| `aws_nat_gateway` | CreateNatGateway (natgw + eip + subnet + vpc) | Describe* | — | DeleteNatGateway | RequestTag; ResourceTag on eip, subnet, vpc |
| `aws_vpc_endpoint` (S3, DynamoDB gateway) | CreateVpcEndpoint | Describe* | ModifyVpcEndpoint | DeleteVpcEndpoints | RequestTag; ResourceTag on vpc, route-table |
| `aws_flow_log` | CreateFlowLogs (+ iam:PassRole to vpc-flow-logs) | Describe* | — | DeleteFlowLogs | RequestTag; ResourceTag on vpc |
| `aws_instance` | RunInstances (instance, image, snapshot, subnet, SG, ENI, volume) | Describe* | ModifyInstanceAttribute, ModifyInstanceMetadataOptions, *IamInstanceProfile*, Start/Stop/Monitor | TerminateInstances | instance: RequestTag + `ec2:InstanceType = t3.micro` + `ec2:MetadataHttpTokens = required`; image/snapshot: `ec2:Owner` in {`099720109477`, `amazon`} (Task 22: IAM evaluates the owner **alias**, and Canonical's Ubuntu AMIs carry the alias `amazon`; the platform's `data.aws_ami` still selects only owner `099720109477`); subnet/SG: ResourceTag; volume/ENI: child, unconditioned; key pairs: not allowed |
| EC2 tags | CreateTags with `ec2:CreateAction` (create-time tagging) | — | CreateTags on ResourceTag resources (Project may not change); DeleteTags | — | DeleteTags needs ResourceTag, explicit tag keys, and may not remove `Project` |
| `aws_lb` | CreateLoadBalancer (+ AddTags) | elasticloadbalancing:Describe* | ModifyLoadBalancerAttributes, SetSecurityGroups, SetSubnets | DeleteLoadBalancer | Name `loadbalancer/app/P-*`; RequestTag on create |
| `aws_lb_target_group` (x2), attachment | CreateTargetGroup, RegisterTargets | Describe* | ModifyTargetGroup(Attributes) | DeleteTargetGroup, DeregisterTargets | Name `targetgroup/P-*`; RequestTag on create |
| `aws_lb_listener`, `aws_lb_listener_rule` | CreateListener, CreateRule | Describe* | ModifyListener(Attributes), ModifyRule, SetRulePriorities | DeleteListener, DeleteRule | Name: listener and rule ARNs under `app/P-*` |
| `aws_ecs_cluster` | CreateCluster (+ ecs:TagResource) | ecs:Describe*/List* | UpdateCluster, UpdateClusterSettings, PutClusterCapacityProviders | DeleteCluster | Name `cluster/P-*`; RequestTag |
| `aws_ecs_task_definition` (app, seed) | RegisterTaskDefinition (+ PassRole to ecs-tasks) | Describe* | (new revision) | DeregisterTaskDefinition | Name `task-definition/P-*`; RequestTag. **Deregister: `Resource "*"` only (no resource-level support)** |
| `aws_ecs_service` | CreateService | Describe* | UpdateService | DeleteService | Name `service/P-*/P-*`; RequestTag. RunTask, ExecuteCommand: not allowed |
| `aws_appautoscaling_target`, `_policy` | RegisterScalableTarget, PutScalingPolicy | application-autoscaling:Describe*; **ListTagsForResource** (Task 26, plan + apply roles) | same | Deregister, DeleteScalingPolicy | `service-namespace = ecs` and `scalable-dimension = ecs:service:DesiredCount` (ARN contains a random ID, so no name scope). Tag read: AWS provider 5.100.0 tags this resource (`@Tags(identifierAttribute="arn")`) and calls ListTagsForResource after create and on every refresh; without it the first apply would fail after creating the service and every later plan would fail. Granted in `platform-read` as a separate statement on `scalable-target/*` in this account and region only (the only resource type AWS lists for the action), not `*` and not `application-autoscaling:*`; image-push does not get it |
| Target-tracking alarms (created by App Auto Scaling as the caller) | cloudwatch:PutMetricAlarm | cloudwatch:Describe* | — | DeleteAlarms | Name `alarm:TargetTracking-service/P-*` |
| `aws_cloudwatch_metric_alarm` | PutMetricAlarm (+ TagResource) | Describe*, ListTagsForResource | same | DeleteAlarms | Name `alarm:P-*`; `cloudwatch:AlarmActions` limited to `sns:R:A:P-*` topics and scaling policies |
| `aws_cloudwatch_dashboard` | PutDashboard | GetDashboard | same | DeleteDashboards | Name `dashboard/P-*` |
| `aws_cloudwatch_log_group` (ECS, flow logs) | CreateLogGroup (+ TagResource) | logs:Describe* | PutRetentionPolicy | DeleteLogGroup | Name `/ecs/P-*`, `/vpc/P-*`; RequestTag |
| `aws_sns_topic`, email subscription | CreateTopic (+ TagResource), Subscribe | Get*/List* | SetTopicAttributes | DeleteTopic, Unsubscribe | Name `sns:R:A:P-*`; RequestTag; Subscribe only `sns:Protocol = email`. Publish: not allowed |
| `aws_dynamodb_table` | dynamodb:* | Describe* | | | Only the table ARN `table/P-production` (unchanged) |
| `aws_s3_bucket` (app) + config, `aws_s3_object` | s3:* | | | | Only `P-app-*` buckets (unchanged) |
| IAM roles, inline policies, instance profile | iam:CreateRole, PutRolePolicy, CreateInstanceProfile, ... | iam:Get*/List* on `P-*` | UpdateAssumeRolePolicy, Tag* | Delete* | Name `role/P-*`, `instance-profile/P-*`. `AttachRolePolicy` removed (not used by the platform) |
| `iam:PassRole` | | | | | `role/P-*` and `iam:PassedToService` in {ecs-tasks, ec2, vpc-flow-logs} (unchanged) |
| Service-linked roles | iam:CreateServiceLinkedRole | | | | `iam:AWSServiceName` in {ecs, elasticloadbalancing, ecs.application-autoscaling} (unchanged) |
| Platform state | s3:PutObject state key, lock file Get/Put/Delete | GetObject, ListBucket | | | Only `P/platform.tfstate(.tflock)` (unchanged) |

## 3. Explicit denies (apply role)

| Sid | Denies | Why |
|---|---|---|
| `DenyOutsideApprovedRegion` | ec2, ecs, elb, app autoscaling, cloudwatch, logs, sns, dynamodb outside `eu-west-2` | Region lock for regional services only. Global services are deliberately absent: IAM requests are signed for us-east-1, Route 53 is global (read-only here), STS `GetCallerIdentity` needs no permission, and S3 is not region-locked here (its bucket ARNs are already exact). CloudWatch dashboards have region-less ARNs, but `PutDashboard` goes to the regional endpoint (`aws:RequestedRegion = eu-west-2`). IAM stays restricted by role/profile ARNs, PassRole service conditions and the explicit deny below; Task 12 tests sign IAM calls as us-east-1 |
| `DenyDataSharingAndUnrelatedNetworking` | Modify{Snapshot,Image}Attribute, Create/Import KeyPair, snapshot/image creation, copy and export, GetPasswordData, serial console, `*VpcPeeringConnection*`, `*TransitGateway*`, VPN and customer gateways, Client VPN | No data sharing, no SSH key material, no network paths to other VPCs or on-premises. Terraform never calls these for this platform |
| `DenyInstanceTypeChange` | ModifyInstanceAttribute that sets an instance type other than t3.micro | Closes the "launch small, resize later" path |
| `DenyPipelineIdentityChanges` | `iam:*` on `role/gha-*`, `policy/gha-*` and `oidc-provider/*` | The apply role cannot change any pipeline role (this or other projects), its own managed policies, or any OIDC provider |
| `ProtectBootstrap` | DeleteBucket, Put/DeleteBucketPolicy, PutBucketVersioning, PutLifecycleConfiguration on the state bucket | Unchanged |

No deny uses a blanket `aws:RequestTag`/`aws:ResourceTag` condition, so none of them can
block Terraform's normal read or create-then-tag sequence.

## 4. Remaining `Resource "*"` allows

| Statement | Actions | Why `*` is unavoidable |
|---|---|---|
| `DescribePlatformServices` (plan + apply) | ec2/elb/ecs/app-autoscaling/cloudwatch/logs Describe*, List*, Get*; ecr Describe*; route53 Get/List | These read APIs do not support resource-level permissions. Read-only |
| `EcsDeregisterTaskDefinitionNoResourceLevel` | ecs:DeregisterTaskDefinition | The service reference lists no resource type for this action. Region-locked by the deny; deregistering only marks a revision INACTIVE (running services keep it) |
| `ServiceLinkedRoles` | iam:CreateServiceLinkedRole | Creation is only possible for the three named services (`iam:AWSServiceName`) |
| `EcrLogin` (image-push) | ecr:GetAuthorizationToken | No resource-level support; pushing is limited to the `legacy-api` repository |

Two statements use a broad ARN instead of `*`: EC2 child resources (`volume/*`,
`network-interface/*`, `security-group-rule/*`, untaggable at request time; the actions
also need a project-tagged parent) and `scalable-target/*` (random IDs, limited by the
ECS namespace and dimension keys). `Ec2RetagProjectResources` uses
`StringEqualsIfExists` on `aws:RequestTag/Project`: tagging a project-owned resource
with other keys is allowed, but a request that includes `Project` must keep it as
`legacy-api`, so ownership cannot be moved.

`Ec2DisassociateStaleEipAssociation` (Task 33, deploy 40577733449). Replacing the EC2
instance terminates the old one first, which releases its Elastic IP. The `aws_eip` update
then calls DisassociateAddress with the association ID it read during the plan. EC2 cannot
resolve an association that no longer exists, so it authorises the call against the
resource `arn:aws:ec2:<region>:<account>:*/*` with no resource tags. No tag-scoped
statement can match that, so the call fails with UnauthorizedOperation. With permission,
EC2 returns `InvalidAssociationID.NotFound`, which the provider treats as success (CloudTrail
shows exactly this for admin callers). The statement allows only that **literal** resource,
written `$${*}/$${*}` in the template and rendered as IAM's `${*}` escape for a literal `*`.
It covers one action and is bound to the region with `aws:RequestedRegion`. The IAM
simulator, run against the live apply role plus this statement, allows it only for the
literal `*/*` in eu-west-2. It does not match a real Elastic IP or network interface, other
regions, other accounts, or any other action. A plain `*` or `*/*` glob would have allowed
DisassociateAddress on every Elastic IP in the account.

## 5. Residual risks and where it may be too restrictive

| Item | Detail |
|---|---|
| Default security group (Task 12 decision) | No longer adopted, so the apply role has **no** access to untagged security groups. IAM cannot narrow that access to the new VPC: `ec2:Vpc` needs the VPC ARN, which is unknown until the VPC exists, and the default SG is untagged. Trade-off: the default SG keeps the AWS default rules (self-referencing ingress, all egress) but nothing uses it (terraform test `workloads_never_use_the_default_security_group`; the apply role cannot launch into an untagged SG). Checkov CKV2_AWS_12 is a reviewed exception (B11). If Security Hub EC2.2 compliance matters, the account owner can strip those rules once, manually, after the VPC exists |
| Child resources | ENIs, volumes and SG rules carry no tag at request time; they are only reachable through actions that also check a project-tagged parent |
| Tag hygiene | `var.tags` in the platform could override `Project`. Then creates fail closed (no access granted) |
| `key_pair_name` | Instance launches with a key pair are no longer authorised. Use SSM Session Manager (the platform already prefers it) |
| `route53_zone_id` / `domain_name` | Route 53 writes were not in the old policy either; setting them still fails at apply |
| Size headroom | The services policy has 564 characters of headroom (6,144 limit). A larger change needs a fourth managed policy (quota: 10 attached per role) |
| Image owner alias (Task 22) | `ec2:Owner` cannot express "Canonical" for aliased images: IAM sees `amazon` for Canonical's official AMIs, so the image check also accepts other AWS-verified public AMIs with that alias (e.g. Amazon Linux). Every other launch condition still applies (t3.micro, IMDSv2, Project tag, project-tagged subnet/SG, no key pairs, eu-west-2, PassRole only to `legacy-api-*`). Tighter option for later: pin the AMI ID in the platform (variable instead of `most_recent = true`) and allow only that image ARN. An exact-ARN policy without pinning is unsafe: the Phase 6 EC2 replacement would pick a newer AMI and be denied after the old instance is destroyed |
| Possible missing actions | The plan cannot prove the write set is complete. Most likely gaps if the provider changes: ELB `ModifyCapacityReservation`/`SetIpAddressType`, ECS `PutAccountSetting`, EC2 tags on new child types. Failure mode is an AccessDenied during apply; fix by adding the single action with the same scope |
| Target tracking alarms | AWS creates them through a forward-access session using the caller's permissions; the name pattern was taken from AWS documentation and not yet observed in this account |
| `instance_type` | Changing the platform's instance type requires changing `legacy_instance_type` in the bootstrap first |

## 6. Approval model (unchanged, fail-closed)

> **Later:** option 1 below was chosen.
>
> - The repository was made public, and the `production` environment was configured with
>   a required reviewer, `main` only and no admin bypass.
> - The gate was verified at runtime ([approval-gate-test.md](approval-gate-test.md)) and
>   then used for every deploy on 27–28 Oct 2026.
> - The text below is the original analysis from 25 Oct.

The repository is **private on GitHub Free**. Environment protection rules (required
reviewers) are not available there. The current workflow is **not** protected by
required reviewers. Three separate things:

| | Who runs it | Credentials | Status |
|---|---|---|---|
| **A. Bootstrap provisioning** (`terraform/bootstrap`: state bucket, ECR, three `gha-*` roles and their policies) | The account owner, manually, from their machine, only after explicit approval of the reviewed plan | The owner's own short-lived session; no GitHub role | Plan ready for review; **not applied** |
| **B. Platform deployment** (`deploy.yml`: image push, plan, apply of `terraform/`) | GitHub Actions, later | OIDC roles created by A | Not run. The apply role trusts only `environment:production`, which gives no enforceable review on a private Free repository |
| **C. Enforceable approval for B** | Decision for the owner | — | Open. Options below; nothing in Tasks 11-12 enables either |

Options for C:
1. **Public repository + protected `production` environment** (required reviewer,
   admins cannot bypass, deployment branch `main`). No IAM change needed. With one
   maintainer this is a self-approval checkpoint, not an independent review
   (docs/ci-cd.md section 5).
2. **Private repository + separately controlled, operator-triggered apply**: the owner
   runs the platform apply (locally, or a dispatch-only workflow that verifies the saved
   plan's SHA-256). The apply role trust would need a reviewed change in
   `terraform/bootstrap`.

Not verified: what GitHub does when a job in a private Free repository references
`environment: production` (fails, or runs without protection). Until C is decided,
`deploy.yml` must not be run, even after A is applied.

## 7. How this was verified (Task 12) and the limits

| Check | Result | Limit |
|---|---|---|
| `terraform test` bootstrap (mocked) | 9 passed (incl. size quotas, attachments, explicit denies) | Mocked provider: checks rendered text, not AWS |
| `terraform test` platform (mocked) | 18 passed (incl. `workloads_never_use_the_default_security_group`) | Same |
| `pytest ci/tests` | 320 passed: IAM model tests incl. 47 realistic-context scenarios, and the rendered-policy gate | The model covers Allow/Deny, wildcards and the operators used here, not all of IAM |
| Rendered-policy gate (`test_rendered_policy_gate.py`, blocking in ci.yml) | Pass; 9 of 9 injected regressions caught (untagged-SG allow, `ec2:*`, unsupported tag key, misspelt action, IfExists, key pair, kms, IAM in region deny, Task 11 oversize layout) | Uses a committed snapshot of the AWS service reference (`ci/tests/data/iam-reference.json`, refreshed by `ci/refresh_iam_reference.py`); a new action fails until the snapshot is refreshed |
| `aws iam simulate-custom-policy` (read-only) | 172 of 172 scenarios match, including IAM signed as us-east-1, EC2/ECS in and outside eu-west-2, untagged, other-project and missing-tag cases, default-SG operations, key pairs, image sharing, PassRole, service-linked roles and OIDC changes | See below. **Not an apply** |
| `aws accessanalyzer validate-policy` (read-only) | 0 findings on all 7 rendered policies | It flags invalid actions and syntax; in a probe it did not flag an unsupported condition key, so the gate's key check is stricter |
| Checkov 3.3.26 | 285 passed, 0 failed, 30 skipped | On templatefile policies only CKV_AWS_62/63 (plus attachment checks) run |
| Fresh bootstrap plan | 23 to add, 0 to change, 0 to destroy | A plan exercises no permission of the roles it creates |

What simulation cannot prove: it evaluates one action against one resource ARN with the
context keys we pass. It does not show that a real request carries those keys, for
example that Terraform's create-time tagging sends `ec2:CreateAction`, that every
taggable create sends `Project` (from provider `default_tags`), that `RunInstances`
authorises the snapshot behind the AMI, or that target-tracking alarm names match
`TargetTracking-service/legacy-api-*`. It also ignores SCPs, permission boundaries and
session policies (none apply to this standalone account today). The first real apply is
the only full test; a gap there shows up as an AccessDenied, not as extra access.
