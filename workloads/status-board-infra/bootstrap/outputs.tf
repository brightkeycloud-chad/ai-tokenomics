output "state_bucket" {
  description = "Bucket holding Terraform state for both layers. Created by create-state-bucket.sh, not by Terraform."
  value       = local.state_bucket
}

output "infra_deploy_role_arn" {
  description = "Role assumed by the infrastructure repository workflows. Set as AWS_ROLE_ARN in that repository."
  value       = aws_iam_role.infra_deploy.arn
}

output "app_deploy_role_arn" {
  description = "Role assumed by the application repository workflows. Set as AWS_ROLE_ARN in that repository."
  value       = aws_iam_role.app_deploy.arn
}

output "agent_role_arn" {
  description = "Read-only role an agent assumes to investigate the stack, for example CloudWatch logs in demo 5.2. Reference it from an AWS CLI profile."
  value       = aws_iam_role.agent.arn
}

output "github_oidc_provider_arn" {
  description = "OIDC provider trusted for GitHub Actions."
  value       = local.github_oidc_provider_arn
}

output "name_prefix" {
  description = "Prefix of every resource name. Set as NAME_PREFIX in both repositories."
  value       = local.name
}

output "region" {
  description = "Region of the state bucket and the stack. Set as AWS_REGION in both repositories."
  value       = var.region
}

output "hosted_zone_name" {
  description = "Hosted zone of the board. Set as HOSTED_ZONE_NAME in the infrastructure repository."
  value       = var.hosted_zone_name
}

output "infra_repo" {
  description = "Infrastructure repository, as owner/name."
  value       = "${var.github_owner}/${var.infra_repo}"
}

output "app_repo" {
  description = "Application repository, as owner/name."
  value       = "${var.github_owner}/${var.app_repo}"
}

output "env" {
  description = "Environment name, the first part of every resource name. Set as ENV_NAME in the infrastructure repository."
  value       = var.env
}

output "oidc_subjects" {
  description = "Subject claims each deployment role accepts. Compare with the sub claim in CloudTrail when a workflow gets AccessDenied."
  value = {
    infra = ["${local.infra_oidc_subject}:ref:refs/heads/main", "${local.infra_oidc_subject}:pull_request"]
    app   = ["${local.app_oidc_subject}:ref:refs/heads/main", "${local.app_oidc_subject}:pull_request"]
  }
}
