# Pipeline IAM permission matrix (Task 11)

Policies: [terraform/bootstrap/policies/](../../terraform/bootstrap/policies/), rendered by
[github_oidc.tf](../../terraform/bootstrap/github_oidc.tf) with ARNs built from names, so
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
| `gha-legacy-api-apply` | `...:environment:production` (unchanged, see section 6) | platform-read, apply-write, plan-store-read |

## 2. Apply role: Terraform resource → API permissions → restriction

| Platform resources | Create | Read (plan/refresh) | Update | Delete | How it is restricted |
|---|---|---|---|---|---|
| `aws_vpc` | CreateVpc | ec2:Describe* (`*`) | ModifyVpcAttribute | DeleteVpc | RequestTag on create; ResourceTag after |
| `aws_subnet` (x4) | CreateSubnet (subnet + vpc) | Describe* | ModifySubnetAttribute | DeleteSubnet | subnet: RequestTag; parent vpc: ResourceTag |
| `aws_internet_gateway` | CreateInternetGateway, Attach (igw + vpc) | Describe* | — | Detach, DeleteInternetGateway | RequestTag; ResourceTag on igw and vpc |
| `aws_route_table`, routes, associations | CreateRouteTable, CreateRoute, AssociateRouteTable | Describe* | ReplaceRoute | DeleteRoute, Disassociate, DeleteRouteTable | RequestTag; ResourceTag on route-table, subnet, vpc, igw |
| `aws_security_group`, `aws_vpc_security_group_*_rule` | CreateSecurityGroup, Authorize* | Describe* | ModifySecurityGroupRules, UpdateSecurityGroupRuleDescriptions* | Revoke*, DeleteSecurityGroup | SG: RequestTag/ResourceTag; `security-group-rule` child: no tag condition (only reachable through a project SG) |
| `aws_default_security_group` | (adopts the VPC's default SG) | Describe* | Revoke*/Authorize*, CreateTags | — | **Exception:** allowed on SGs with **no** Project tag (the default SG is untagged until Terraform tags it). See section 5 |
| `aws_eip` (x2) | AllocateAddress | Describe* | AssociateAddress, DisassociateAddress | ReleaseAddress | RequestTag; ResourceTag on eip, instance; ENI child unconditioned |
| `aws_nat_gateway` | CreateNatGateway (natgw + eip + subnet + vpc) | Describe* | — | DeleteNatGateway | RequestTag; ResourceTag on eip, subnet, vpc |
| `aws_vpc_endpoint` (S3, DynamoDB gateway) | CreateVpcEndpoint | Describe* | ModifyVpcEndpoint | DeleteVpcEndpoints | RequestTag; ResourceTag on vpc, route-table |
| `aws_flow_log` | CreateFlowLogs (+ iam:PassRole to vpc-flow-logs) | Describe* | — | DeleteFlowLogs | RequestTag; ResourceTag on vpc |
| `aws_instance` | RunInstances (instance, image, snapshot, subnet, SG, ENI, volume) | Describe* | ModifyInstanceAttribute, ModifyInstanceMetadataOptions, *IamInstanceProfile*, Start/Stop/Monitor | TerminateInstances | instance: RequestTag + `ec2:InstanceType = t3.micro` + `ec2:MetadataHttpTokens = required`; image/snapshot: `ec2:Owner = 099720109477` (Canonical); subnet/SG: ResourceTag; volume/ENI: child, unconditioned; key pairs: not allowed |
| EC2 tags | CreateTags with `ec2:CreateAction` (create-time tagging) | — | CreateTags on ResourceTag resources (Project may not change); DeleteTags | — | DeleteTags needs ResourceTag, explicit tag keys, and may not remove `Project` |
| `aws_lb` | CreateLoadBalancer (+ AddTags) | elasticloadbalancing:Describe* | ModifyLoadBalancerAttributes, SetSecurityGroups, SetSubnets | DeleteLoadBalancer | Name `loadbalancer/app/P-*`; RequestTag on create |
| `aws_lb_target_group` (x2), attachment | CreateTargetGroup, RegisterTargets | Describe* | ModifyTargetGroup(Attributes) | DeleteTargetGroup, DeregisterTargets | Name `targetgroup/P-*`; RequestTag on create |
| `aws_lb_listener`, `aws_lb_listener_rule` | CreateListener, CreateRule | Describe* | ModifyListener(Attributes), ModifyRule, SetRulePriorities | DeleteListener, DeleteRule | Name: listener and rule ARNs under `app/P-*` |
| `aws_ecs_cluster` | CreateCluster (+ ecs:TagResource) | ecs:Describe*/List* | UpdateCluster, UpdateClusterSettings, PutClusterCapacityProviders | DeleteCluster | Name `cluster/P-*`; RequestTag |
| `aws_ecs_task_definition` (app, seed) | RegisterTaskDefinition (+ PassRole to ecs-tasks) | Describe* | (new revision) | DeregisterTaskDefinition | Name `task-definition/P-*`; RequestTag. **Deregister: `Resource "*"` only (no resource-level support)** |
| `aws_ecs_service` | CreateService | Describe* | UpdateService | DeleteService | Name `service/P-*/P-*`; RequestTag. RunTask, ExecuteCommand: not allowed |
| `aws_appautoscaling_target`, `_policy` | RegisterScalableTarget, PutScalingPolicy | application-autoscaling:Describe* | same | Deregister, DeleteScalingPolicy | `service-namespace = ecs` and `scalable-dimension = ecs:service:DesiredCount` (ARN contains a random ID, so no name scope) |
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
| `DenyOutsideApprovedRegion` | ec2, ecs, elb, app autoscaling, cloudwatch, logs, sns, dynamodb outside `eu-west-2` | Region lock (IAM is global and not included) |
| `DenyDataSharingAndUnrelatedNetworking` | Modify{Snapshot,Image}Attribute, Create/Import KeyPair, snapshot/image creation, copy and export, GetPasswordData, serial console, `*VpcPeeringConnection*`, `*TransitGateway*`, VPN and customer gateways, Client VPN | No data sharing, no SSH key material, no network paths to other VPCs or on-premises. Terraform never calls these for this platform |
| `DenyInstanceTypeChange` | ModifyInstanceAttribute that sets an instance type other than t3.micro | Closes the "launch small, resize later" path |
| `DenyPipelineIdentityChanges` | `iam:*` on `role/gha-*` and `oidc-provider/*` | The apply role cannot change any pipeline role (this or other projects) or any OIDC provider |
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

## 5. Residual risks and where it may be too restrictive

| Item | Detail |
|---|---|
| Untagged security groups | `aws_default_security_group` needs Revoke/Authorize/CreateTags on an untagged SG. The same statements allow that on **any untagged SG in eu-west-2** (today: the default VPC's default SG). Alternative: drop `aws_default_security_group` (Checkov CKV2_AWS_12 would then need an exception) |
| Child resources | ENIs, volumes and SG rules carry no tag at request time; they are only reachable through actions that also check a project-tagged parent |
| Tag hygiene | `var.tags` in the platform could override `Project`. Then creates fail closed (no access granted) |
| `key_pair_name` | Instance launches with a key pair are no longer authorised. Use SSM Session Manager (the platform already prefers it) |
| `route53_zone_id` / `domain_name` | Route 53 writes were not in the old policy either; setting them still fails at apply |
| Possible missing actions | The plan cannot prove the write set is complete. Most likely gaps if the provider changes: ELB `ModifyCapacityReservation`/`SetIpAddressType`, ECS `PutAccountSetting`, EC2 tags on new child types. Failure mode is an AccessDenied during apply; fix by adding the single action with the same scope |
| Target tracking alarms | AWS creates them through a forward-access session using the caller's permissions; the name pattern was taken from AWS documentation and not yet observed in this account |
| `instance_type` | Changing the platform's instance type requires changing `legacy_instance_type` in the bootstrap first |

## 6. Approval model (unchanged, fail-closed)

The repository is **private on GitHub Free**. Environment protection rules (required
reviewers) are not available there, so the apply role's trust
(`environment:production`) is left unchanged and `deploy.yml` is not to be run. Nothing
in Task 11 enables either option below:

1. **Public repository + protected `production` environment** (required reviewer,
   admins cannot bypass, deployment branch `main`). No IAM change needed. Self-approval
   is a checkpoint, not an independent review (docs/ci-cd.md section 5).
2. **Private repository + separately controlled, operator-triggered apply**: a
   dispatch-only apply run by the account owner that verifies the saved plan's SHA-256,
   or a local apply with the owner's own credentials. The apply role trust would need a
   reviewed change in `terraform/bootstrap`.

Not verified: what GitHub does when a job in a private Free repository references
`environment: production` (fails, or runs without protection). If it runs, a token
with the `environment:production` subject could be issued without review. Decide on
option 1 or 2 **before** applying the bootstrap, and do not run `deploy.yml` until then.

## 7. How this was verified (and the limits)

| Check | Result | Limit |
|---|---|---|
| `terraform test` (bootstrap, mocked) | 8 passed (3 new: policies known at plan, no service-wide write, plan/image roles narrow) | Mocked provider: checks the rendered text, not AWS |
| `ci/tests/test_iam_policies.py` | 132 new cases pass (181 total in ci/tests) | A small IAM model (Allow/Deny, wildcards, the operators used here), not AWS |
| Service reference cross-check | Every action name in the templates exists; every service-specific condition key applies to at least one action in its statement | Static check against the JSON, run from the scratchpad (not in CI) |
| `aws iam simulate-custom-policy` | 127 of 127 scenarios match the expected decision | Read-only API. Simulates one action and one resource per call with the context we supply; it does not run Terraform, check that a real request carries those keys, or evaluate SCPs, permission boundaries or session policies. **It is not an apply** |
| Checkov 3.3.26 | 280 passed, 0 failed, 29 skipped | On templatefile policies it only runs CKV_AWS_62/63 |
| Fresh bootstrap plan | 18 to add, 0 to change, 0 to destroy; all 7 role policies fully visible | A plan does not exercise any permission of the roles it creates |
