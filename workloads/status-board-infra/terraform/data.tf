data "aws_availability_zones" "available" {
  state = "available"

  # Standard Availability Zones only. In an account with Local Zones opted in,
  # their names can sort ahead of us-east-1a, so an unfiltered list hands back
  # Local Zones. The load balancer then refuses the subnets with "You cannot
  # provide subnets from multiple locales". Local Zones and Wavelength zones
  # require opt-in, standard zones do not, which is what this filter keys on.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

data "aws_caller_identity" "current" {}

# The certificate that covers subdomain.hosted_zone_name. Looked up rather than
# hard-coded, so a renewed certificate is picked up without a code change. By
# default this matches a certificate whose primary name is the zone apex, with
# a wildcard alternative name. Set certificate_domain to match another one.
data "aws_acm_certificate" "board" {
  domain      = local.certificate_domain
  statuses    = ["ISSUED"]
  most_recent = true
}

data "aws_route53_zone" "board" {
  name         = "${var.hosted_zone_name}."
  private_zone = false
}
