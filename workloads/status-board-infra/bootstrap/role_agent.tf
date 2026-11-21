# The role an agent assumes to investigate the running stack, for example to
# read CloudWatch logs and metrics in demo 5.2.
#
# This role is tool scoping written in IAM. The agent can read CloudWatch,
# CloudTrail, AWS Config and the running resources, and it cannot change any
# of them. Read-only is not a precaution here, it is the
# point: the blast radius of an agent is whatever you allowed it to reach.
resource "aws_iam_role" "agent" {
  name        = "${local.name}-agent"
  description = "Read-only investigation role for the demonstration agent."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${local.account_id}:root" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${local.name}-agent" }
}

data "aws_iam_policy_document" "agent" {
  # The evidence trail: what happened, and what the resource looked like
  # before and after.
  statement {
    sid    = "ReadTheEvidence"
    effect = "Allow"
    actions = [
      "cloudtrail:LookupEvents",
      "cloudtrail:DescribeTrails",
      "cloudtrail:GetTrailStatus",
      "config:SelectResourceConfig",
      "config:SelectAggregateResourceConfig",
      "config:GetResourceConfigHistory",
      "config:ListDiscoveredResources",
      "config:BatchGetResourceConfig",
      "config:DescribeConfigurationRecorders",
      "config:DescribeConfigurationRecorderStatus",
    ]
    resources = ["*"]
  }

  # The current state of the running environment.
  statement {
    sid    = "DescribeTheEnvironment"
    effect = "Allow"
    actions = [
      "ec2:Describe*",
      "ecs:Describe*",
      "ecs:List*",
      "elasticloadbalancing:Describe*",
      "dynamodb:DescribeTable",
      "dynamodb:ListTables",
      "ecr:Describe*",
      "route53:Get*",
      "route53:List*",
      "acm:Describe*",
      "acm:List*",
    ]
    resources = ["*"]
  }

  # Application logs, to confirm the symptom.
  statement {
    sid    = "ReadApplicationLogs"
    effect = "Allow"
    actions = [
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:GetLogEvents",
      "logs:FilterLogEvents",
      "logs:StartQuery",
      "logs:GetQueryResults",
    ]
    resources = ["*"]
  }

  # Metrics and alarms, to show when the symptom began.
  statement {
    sid    = "ReadMetricsAndAlarms"
    effect = "Allow"
    actions = [
      "cloudwatch:ListMetrics",
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:DescribeAlarmHistory",
    ]
    resources = ["*"]
  }

  # Everything that could change the account is refused outright, whatever any
  # other policy might allow. An agent that is talked into trying a fix gets
  # an explicit denial rather than a surprise.
  statement {
    sid    = "DenyEveryWrite"
    effect = "Deny"
    actions = [
      "ec2:Authorize*",
      "ec2:Revoke*",
      "ec2:Create*",
      "ec2:Delete*",
      "ec2:Modify*",
      "ecs:Update*",
      "ecs:Delete*",
      "ecs:Create*",
      "elasticloadbalancing:Create*",
      "elasticloadbalancing:Delete*",
      "elasticloadbalancing:Modify*",
      "elasticloadbalancing:Set*",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:DeleteTable",
      "iam:*",
      "route53:Change*",
      "cloudwatch:Put*",
      "cloudwatch:Set*",
      "cloudwatch:Delete*",
      "s3:Put*",
      "s3:Delete*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "agent" {
  name   = "${local.name}-agent"
  role   = aws_iam_role.agent.id
  policy = data.aws_iam_policy_document.agent.json
}
