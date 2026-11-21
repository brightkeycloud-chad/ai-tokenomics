variable "region" {
  description = "AWS region that hosts the status board."
  type        = string
  default     = "us-east-1"
}

variable "env" {
  description = "Environment name, used as the first part of every resource name. Must match the env used in ../bootstrap."
  type        = string
  default     = "tokenomics"
}

variable "service" {
  description = "Service name, used as the second part of every resource name."
  type        = string
  default     = "status-board"
}

variable "costcenter" {
  description = "Cost centre the running charges are attributed to."
  type        = string
  default     = "tokenomics"
}

variable "repo_name" {
  description = "Name of the repository that holds this Terraform. Applied as the repo tag on every resource."
  type        = string
  default     = "tokenomics-status-board-infra"
}

variable "hosted_zone_name" {
  description = "Public Route 53 hosted zone that the board's record is created in. Change this to a zone in your own account."
  type        = string
  default     = "awsf.brightkey.cloud"
}

variable "certificate_domain" {
  description = "Primary domain name of an ISSUED ACM certificate in this region that covers subdomain.hosted_zone_name. Leave empty to use hosted_zone_name, which suits a certificate for the zone apex with a wildcard alternative name."
  type        = string
  default     = ""
}

variable "subdomain" {
  description = "Single label placed in front of hosted_zone_name. The wildcard certificate covers one label only, so this cannot contain a dot."
  type        = string
  default     = "tokenomics"

  validation {
    condition     = !strcontains(var.subdomain, ".")
    error_message = "subdomain must be a single label; the wildcard certificate does not cover nested subdomains."
  }
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC. Avoids 10.0.0.0/16, which is often in use already."
  type        = string
  default     = "10.20.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block, for example 10.20.0.0/16."
  }
}

variable "az_count" {
  description = "How many Availability Zones to spread the subnets across. The ALB requires at least two."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 4
    error_message = "az_count must be between 2 and 4; an Application Load Balancer requires at least two."
  }
}

variable "image_tag" {
  description = "Tag of the application image in ECR that the task definition runs."
  type        = string
  default     = "latest"
}

variable "desired_count" {
  description = "How many copies of the application task to run."
  type        = number
  default     = 1
}

variable "task_cpu" {
  description = "CPU units for the task. 256 is a quarter of a vCPU."
  type        = number
  default     = 256
}

variable "task_memory" {
  description = "Memory in MiB for the task."
  type        = number
  default     = 512
}

variable "container_port" {
  description = "Port the application listens on inside the container."
  type        = number
  default     = 8080
}

variable "deployment_minimum_healthy_percent" {
  description = "Lowest percentage of the desired count ECS must keep running during a deployment. Keep at 100. Zero was measured and was slower: ECS waits for the old task to terminate before provisioning the replacement, which cost 1m43s of dead time."
  type        = number
  default     = 100

  validation {
    condition     = var.deployment_minimum_healthy_percent >= 0 && var.deployment_minimum_healthy_percent <= 100
    error_message = "deployment_minimum_healthy_percent must be between 0 and 100."
  }
}

variable "deployment_maximum_percent" {
  description = "Highest percentage of the desired count ECS may run during a deployment. Keep at 200, which lets the replacement task start while the old one is still serving. Setting 100 also forces Availability Zone Rebalancing off, since that feature needs headroom."
  type        = number
  default     = 200

  validation {
    condition     = var.deployment_maximum_percent >= 100 && var.deployment_maximum_percent <= 200
    error_message = "deployment_maximum_percent must be between 100 and 200."
  }
}

variable "health_check_interval_seconds" {
  description = "Seconds between target health checks. AWS allows 5 to 300. The shortest interval gets a new task into service soonest, which matters when the demonstration is rebuilt against the clock."
  type        = number
  default     = 5

  validation {
    condition     = var.health_check_interval_seconds >= 5 && var.health_check_interval_seconds <= 300
    error_message = "health_check_interval_seconds must be between 5 and 300."
  }
}

variable "health_check_timeout_seconds" {
  description = "Seconds before a health check counts as failed. AWS allows 2 to 120, and it must be under the interval."
  type        = number
  default     = 2

  validation {
    condition     = var.health_check_timeout_seconds >= 2 && var.health_check_timeout_seconds <= 120
    error_message = "health_check_timeout_seconds must be between 2 and 120."
  }
}

variable "deregistration_delay_seconds" {
  description = "How long the load balancer drains connections from a task being replaced. The AWS default of 300 adds five minutes to every deployment for no benefit here."
  type        = number
  default     = 5

  validation {
    condition     = var.deregistration_delay_seconds >= 0 && var.deregistration_delay_seconds <= 3600
    error_message = "deregistration_delay_seconds must be between 0 and 3600."
  }
}

variable "log_retention_days" {
  description = "How long to keep application logs in CloudWatch."
  type        = number
  default     = 7
}

# The API rejects a timeout that is not shorter than the interval. Failing at
# plan time is friendlier than failing part way through an apply.
check "health_check_timing" {
  assert {
    condition     = var.health_check_timeout_seconds < var.health_check_interval_seconds
    error_message = "health_check_timeout_seconds must be less than health_check_interval_seconds."
  }
}
