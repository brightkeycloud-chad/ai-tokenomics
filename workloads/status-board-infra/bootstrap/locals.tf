locals {
  name = "${var.env}-${var.service}"

  # GitHub embeds immutable numeric ids in the OIDC subject claim, so that
  # renaming or transferring a repository cannot carry this trust with it.
  # Confirmed from CloudTrail, the presented subject looks like:
  #   repo:<owner>@<owner_id>/<repo>@<repo_id>:ref:refs/heads/main
  # Matching the plain owner/repo form alone fails with AccessDenied.
  oidc_owner = "${var.github_owner}@${var.github_owner_id}"

  # The value GitHub reports for each repository wins over the computed form.
  infra_oidc_subject = coalesce(var.infra_oidc_subject_prefix, "repo:${local.oidc_owner}/${var.infra_repo}@${var.infra_repo_id}")
  app_oidc_subject   = coalesce(var.app_oidc_subject_prefix, "repo:${local.oidc_owner}/${var.app_repo}@${var.app_repo_id}")
  account_id         = data.aws_caller_identity.current.account_id

  state_bucket = "${var.env}-${var.service}-tfstate-${data.aws_caller_identity.current.account_id}"

  # Resource name prefix the deployment role is allowed to manage IAM roles
  # under. Anything outside this prefix is out of reach.
  managed_role_prefix = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.env}-${var.service}-*"
}
