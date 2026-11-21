locals {
  # Every resource is named {env}-{service}-{resource}, for example
  # tokenomics-status-board-alb.
  name = "${var.env}-${var.service}"

  certificate_domain = coalesce(var.certificate_domain, var.hosted_zone_name)

  # Take the first az_count Availability Zones the region offers, rather than
  # naming them, so the stack is portable between regions.
  azs = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # Public subnets carry the load balancer. Private subnets carry the tasks.
  public_subnets  = { for i, az in local.azs : az => cidrsubnet(var.vpc_cidr, 8, i) }
  private_subnets = { for i, az in local.azs : az => cidrsubnet(var.vpc_cidr, 8, i + 10) }
}
