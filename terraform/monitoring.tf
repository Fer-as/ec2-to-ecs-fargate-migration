# CloudWatch alarms and a small migration dashboard.
#
# Metric namespaces and dimensions:
#   AWS/ApplicationELB  LoadBalancer (+ TargetGroup)   = arn_suffix values
#   AWS/ECS             ClusterName, ServiceName       CPUUtilization, MemoryUtilization
#   ECS/ContainerInsights ClusterName, ServiceName     RunningTaskCount, DesiredTaskCount
# Missing data is "not breaching": no requests (or no service yet) is not an incident.

locals {
  lb_dim        = aws_lb.app.arn_suffix
  tg_ecs_dim    = aws_lb_target_group.ecs.arn_suffix
  tg_ec2_dim    = aws_lb_target_group.ec2.arn_suffix
  alarm_actions = [aws_sns_topic.alarms.arn]
}

resource "aws_sns_topic" "alarms" {
  name              = "${local.name}-alarms"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count     = var.alarm_email == null ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email # must be confirmed from the email AWS sends
}

resource "aws_cloudwatch_metric_alarm" "ecs_cpu_high" {
  alarm_name          = "${local.name}-ecs-cpu-high"
  alarm_description   = "ECS service average CPU above 80% for 3 minutes"
  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  dimensions          = { ClusterName = local.cluster_name, ServiceName = local.service_name }
  statistic           = "Average"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "ecs_target_5xx" {
  alarm_name          = "${local.name}-ecs-target-5xx"
  alarm_description   = "ECS targets returned more than 5 HTTP 5xx per minute in 2 of 3 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = local.lb_dim, TargetGroup = local.tg_ecs_dim }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "unhealthy_targets" {
  for_each = {
    ecs = local.tg_ecs_dim
    ec2 = local.tg_ec2_dim
  }

  alarm_name          = "${local.name}-${each.key}-unhealthy-targets"
  alarm_description   = "At least one ${upper(each.key)} target failed the ALB /ready health check for 2 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = { LoadBalancer = local.lb_dim, TargetGroup = each.value }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
  alarm_name          = "${local.name}-alb-5xx"
  alarm_description   = "The ALB itself returned more than 5 HTTP 5xx per minute (e.g. no healthy target)"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_ELB_5XX_Count"
  dimensions          = { LoadBalancer = local.lb_dim }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "ecs_tasks_below_desired" {
  count               = var.enable_container_insights ? 1 : 0
  alarm_name          = "${local.name}-ecs-tasks-below-desired"
  alarm_description   = "Fewer ECS tasks running than desired for 5 minutes"
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions

  metric_query {
    id          = "missing"
    expression  = "desired - running"
    label       = "Desired minus running tasks"
    return_data = true
  }

  metric_query {
    id = "desired"
    metric {
      namespace   = "ECS/ContainerInsights"
      metric_name = "DesiredTaskCount"
      dimensions  = { ClusterName = local.cluster_name, ServiceName = local.service_name }
      stat        = "Average"
      period      = 60
    }
  }

  metric_query {
    id = "running"
    metric {
      namespace   = "ECS/ContainerInsights"
      metric_name = "RunningTaskCount"
      dimensions  = { ClusterName = local.cluster_name, ServiceName = local.service_name }
      stat        = "Average"
      period      = 60
    }
  }
}

# --- Dashboard ---------------------------------------------------------------------------------

locals {
  alb_ns = "AWS/ApplicationELB"

  # One line per target group, so the EC2/ECS split is visible side by side
  per_tg = { for k, tg in { EC2 = local.tg_ec2_dim, ECS = local.tg_ecs_dim } :
  k => { TargetGroup = tg, LoadBalancer = local.lb_dim } }

  dashboard_widgets = [
    {
      title   = "Requests by target group (traffic split)", stat = "Sum",
      metrics = [for k, d in local.per_tg : [local.alb_ns, "RequestCount", "TargetGroup", d.TargetGroup, "LoadBalancer", d.LoadBalancer, { label = k }]]
    },
    # Each metric must stay its own array: CloudWatch rejects a flat list. flatten() is
    # recursive and would also flatten the metric arrays, so concat() per metric kind.
    {
      title = "Healthy / unhealthy targets", stat = "Minimum",
      metrics = concat(
        [for k, d in local.per_tg : [local.alb_ns, "HealthyHostCount", "TargetGroup", d.TargetGroup, "LoadBalancer", d.LoadBalancer, { label = "${k} healthy" }]],
        [for k, d in local.per_tg : [local.alb_ns, "UnHealthyHostCount", "TargetGroup", d.TargetGroup, "LoadBalancer", d.LoadBalancer, { label = "${k} unhealthy", stat = "Maximum" }]],
      )
    },
    {
      title = "HTTP 4xx / 5xx", stat = "Sum",
      metrics = concat(
        [for k, d in local.per_tg : [local.alb_ns, "HTTPCode_Target_5XX_Count", "TargetGroup", d.TargetGroup, "LoadBalancer", d.LoadBalancer, { label = "${k} target 5xx" }]],
        [for k, d in local.per_tg : [local.alb_ns, "HTTPCode_Target_4XX_Count", "TargetGroup", d.TargetGroup, "LoadBalancer", d.LoadBalancer, { label = "${k} target 4xx" }]],
        [[local.alb_ns, "HTTPCode_ELB_5XX_Count", "LoadBalancer", local.lb_dim, { label = "ALB 5xx" }]],
      )
    },
    {
      title   = "Target response time p95 (s)", stat = "p95",
      metrics = [for k, d in local.per_tg : [local.alb_ns, "TargetResponseTime", "TargetGroup", d.TargetGroup, "LoadBalancer", d.LoadBalancer, { label = k }]]
    },
    {
      title = "ECS CPU / memory (%)", stat = "Average",
      metrics = [
        ["AWS/ECS", "CPUUtilization", "ClusterName", local.cluster_name, "ServiceName", local.service_name, { label = "CPU" }],
        ["AWS/ECS", "MemoryUtilization", "ClusterName", local.cluster_name, "ServiceName", local.service_name, { label = "Memory" }],
      ]
    },
    {
      title = "ECS tasks: desired vs running", stat = "Average",
      metrics = [
        ["ECS/ContainerInsights", "DesiredTaskCount", "ClusterName", local.cluster_name, "ServiceName", local.service_name, { label = "Desired" }],
        ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", local.cluster_name, "ServiceName", local.service_name, { label = "Running" }],
      ]
    },
  ]
}

resource "aws_cloudwatch_dashboard" "migration" {
  dashboard_name = "${local.name}-migration"

  dashboard_body = jsonencode({
    widgets = [for i, w in local.dashboard_widgets : {
      type   = "metric"
      x      = (i % 2) * 12
      y      = floor(i / 2) * 6
      width  = 12
      height = 6
      properties = {
        title   = w.title
        region  = var.aws_region
        view    = "timeSeries"
        stacked = false
        period  = 60
        stat    = w.stat
        metrics = w.metrics
      }
    }]
  })
}
