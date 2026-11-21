data "aws_caller_identity" "current" {}

data "aws_route53_zone" "board" {
  name         = "${var.hosted_zone_name}."
  private_zone = false
}
