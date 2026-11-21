# Assumed by workflows in status-board-app. It can publish an image and roll
# the service forward. It cannot touch the network, the database or the load
# balancer.
resource "aws_iam_role" "app_deploy" {
  name               = "${local.name}-app-deploy"
  description        = "Publishes the application image and updates the ECS service."
  assume_role_policy = data.aws_iam_policy_document.github_trust_app.json

  tags = { Name = "${local.name}-app-deploy" }
}

data "aws_iam_policy_document" "app_deploy" {
  statement {
    sid       = "EcrLogin"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Push only to this stack's repository.
  statement {
    sid    = "PushImageToStackRepository"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
    ]
    resources = ["arn:aws:ecr:${var.region}:${local.account_id}:repository/${local.name}"]
  }

  # Register a new task definition revision and roll the service onto it.
  statement {
    sid    = "UpdateStackService"
    effect = "Allow"
    actions = [
      "ecs:RegisterTaskDefinition",
      "ecs:DescribeTaskDefinition",
      "ecs:DescribeServices",
      "ecs:UpdateService",
      "ecs:ListTasks",
      "ecs:DescribeTasks",
    ]
    resources = ["*"]
  }

  # Load the demonstration's sample services after a deployment, so that a
  # rebuilt environment always comes up showing the same board. Confined to the
  # two tables this stack owns, and write-only in the sense that it cannot
  # delete anything or touch a table's structure.
  statement {
    sid    = "SeedStackTables"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
    ]
    resources = [
      "arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.name}-services",
      "arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.name}-incidents",
    ]
  }

  # Hand the task its roles, and no others.
  statement {
    sid       = "PassStackTaskRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [local.managed_role_prefix]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "app_deploy" {
  name   = "${local.name}-app-deploy"
  role   = aws_iam_role.app_deploy.id
  policy = data.aws_iam_policy_document.app_deploy.json
}
