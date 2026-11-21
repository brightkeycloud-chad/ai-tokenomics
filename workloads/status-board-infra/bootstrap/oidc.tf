# Lets GitHub Actions exchange a workflow identity token for temporary AWS
# credentials. No access key is ever created, stored or rotated.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  tags = { Name = "${local.name}-github-oidc" }
}

# One provider per URL per account. If another stack already created it, use
# that one instead of failing on a duplicate.
data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1

  url = "https://token.actions.githubusercontent.com"
}

locals {
  github_oidc_provider_arn = (var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
  : data.aws_iam_openid_connect_provider.github[0].arn)
}

# Trust document shared by both deployment roles. Each role names the
# repository and the refs it accepts, so a workflow in any other repository,
# or on an unexpected branch, is refused.
data "aws_iam_policy_document" "github_trust_infra" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "${local.infra_oidc_subject}:ref:refs/heads/main",
        "${local.infra_oidc_subject}:pull_request",
      ]
    }
  }
}

data "aws_iam_policy_document" "github_trust_app" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "${local.app_oidc_subject}:ref:refs/heads/main",
        "${local.app_oidc_subject}:pull_request",
      ]
    }
  }
}
