resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days

  tags = { Name = "${local.name}-logs" }
}

resource "aws_ecs_cluster" "this" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = { Name = local.name }
}

resource "aws_ecs_task_definition" "this" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  # Graviton. The image must be built for the same architecture.
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name      = "app"
    image     = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
    essential = true

    portMappings = [{
      containerPort = var.container_port
      protocol      = "tcp"
    }]

    environment = [
      { name = "SERVICES_TABLE", value = aws_dynamodb_table.services.name },
      { name = "INCIDENTS_TABLE", value = aws_dynamodb_table.incidents.name },
      { name = "AWS_REGION", value = var.region },
      { name = "PORT", value = tostring(var.container_port) },
      { name = "LOG_LEVEL", value = "INFO" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "app"
      }
    }
  }])

  tags = { Name = local.name }
}

resource "aws_ecs_service" "this" {
  name            = local.name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  # Overlap the replacement with the task it replaces. Setting 0 and 100 to
  # avoid the overlap was measured and came out slower, not faster: ECS stops
  # the old task, then takes over a minute and a half to provision the
  # replacement, where overlapping starts it in twelve seconds. The rollout
  # sequencing is ECS's own and is not avoidable from here.
  deployment_minimum_healthy_percent = var.deployment_minimum_healthy_percent
  deployment_maximum_percent         = var.deployment_maximum_percent

  # Availability Zone Rebalancing is on by default and refuses a maximum
  # percent of 100, because it needs headroom to move a task. With one task
  # there is nothing to rebalance, so it is switched off when the maximum
  # leaves no headroom, and back on when it does.
  availability_zone_rebalancing = var.deployment_maximum_percent <= 100 ? "DISABLED" : "ENABLED"

  network_configuration {
    subnets          = [for subnet in aws_subnet.private : subnet.id]
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = "app"
    container_port   = var.container_port
  }

  # The application deployment workflow registers new task definition
  # revisions. Terraform should not roll them back on the next apply.
  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [
    aws_lb_listener.https,
    aws_iam_role_policy.task_dynamodb,
  ]

  tags = { Name = local.name }
}
