resource "aws_security_group" "alb" {
  name        = "${local.name}-alb-sg"
  description = "Accepts web traffic from the internet for the status board."
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name}-alb-sg" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from anywhere"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "alb_http_redirect" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from anywhere, redirected to HTTPS"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Reach the application tasks"
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task-sg"
  description = "Accepts traffic from the load balancer only."
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name}-task-sg" }
}

# The only path into the application. The load balancer reaches the tasks on
# this rule and nothing else does.
resource "aws_vpc_security_group_ingress_rule" "task_from_alb" {
  security_group_id            = aws_security_group.task.id
  description                  = "Application port, from the load balancer only"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
}

# Outbound through the regional NAT gateway, to pull the image from ECR, reach
# DynamoDB and ship logs.
resource "aws_vpc_security_group_egress_rule" "task_all" {
  security_group_id = aws_security_group.task.id
  description       = "Outbound to AWS services through the NAT gateway"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
