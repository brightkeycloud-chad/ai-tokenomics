resource "aws_lb" "this" {
  name               = "${local.name}-alb"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = [for subnet in aws_subnet.public : subnet.id]

  # Left off so the demonstration can be torn down without emptying a bucket
  # first. Access logs are not part of the evidence trail this course uses.
  enable_deletion_protection = false

  tags = { Name = "${local.name}-alb" }
}

resource "aws_lb_target_group" "this" {
  name        = "${local.name}-tg"
  port        = var.container_port
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.this.id

  # The default is 300 seconds, which makes every deployment wait out a five
  # minute connection drain on the task being replaced. There is no real
  # traffic on a demonstration board, so that is five minutes of nothing.
  deregistration_delay = var.deregistration_delay_seconds

  # /healthz answers for the process alone and never touches DynamoDB, so a
  # failing check here means the load balancer cannot reach the task.
  health_check {
    path     = "/healthz"
    matcher  = "200"
    interval = var.health_check_interval_seconds
    timeout  = var.health_check_timeout_seconds

    # Two is the lowest an Application Load Balancer accepts, so at a five
    # second interval a new task is in service about ten seconds after it
    # answers, rather than thirty.
    healthy_threshold   = 2
    unhealthy_threshold = 2
  }

  # Let a replacement target group be created before the old one is removed,
  # since the listener holds a reference to it.
  lifecycle {
    create_before_destroy = true
  }

  tags = { Name = "${local.name}-tg" }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = data.aws_acm_certificate.board.arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_route53_record" "board" {
  zone_id = data.aws_route53_zone.board.zone_id
  name    = "${var.subdomain}.${var.hosted_zone_name}"
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}
