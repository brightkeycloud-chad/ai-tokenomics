# Assumed by workflows in status-board-infra to deploy the application stack.
resource "aws_iam_role" "infra_deploy" {
  name               = "${local.name}-tf-deploy"
  description        = "Deploys the status board stack from GitHub Actions."
  assume_role_policy = data.aws_iam_policy_document.github_trust_infra.json

  tags = { Name = "${local.name}-tf-deploy" }
}

data "aws_iam_policy_document" "infra_deploy" {
  # Networking, load balancing and the container platform. These services offer
  # little useful resource-level scoping on the create calls this stack makes,
  # so they are granted at service level and bounded by the role being
  # assumable only from one repository.
  statement {
    sid    = "NetworkAndPlatform"
    effect = "Allow"
    actions = [
      "ec2:*",
      "elasticloadbalancing:*",
      "ecs:*",
      "ecr:*",
      "dynamodb:*",
      "logs:*",
      "application-autoscaling:*",
    ]
    resources = ["*"]
  }

  # Read-only lookups the stack performs: the certificate and the zone.
  statement {
    sid    = "ReadCertificatesAndZones"
    effect = "Allow"
    actions = [
      "acm:DescribeCertificate",
      "acm:GetCertificate",
      "acm:ListCertificates",
      "acm:ListTagsForCertificate",
      "route53:GetHostedZone",
      "route53:ListHostedZones",
      "route53:ListHostedZonesByName",
      "route53:GetChange",
    ]
    resources = ["*"]
  }

  # Record changes are confined to the one hosted zone the board lives in.
  statement {
    sid    = "WriteBoardDnsRecordsOnly"
    effect = "Allow"
    actions = [
      "route53:ChangeResourceRecordSets",
      "route53:ListResourceRecordSets",
      "route53:ListTagsForResource",
    ]
    resources = ["arn:aws:route53:::hostedzone/${data.aws_route53_zone.board.zone_id}"]
  }

  # Role management is confined to roles named for this stack. The role cannot
  # create or alter any other role in the account, including its own.
  statement {
    sid    = "ManageStackRolesOnly"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:PassRole",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:ListRoleTags",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = [local.managed_role_prefix]
  }

  # Creating the ECS service needs the service-linked role to exist.
  statement {
    sid       = "CreateEcsServiceLinkedRole"
    effect    = "Allow"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["ecs.amazonaws.com"]
    }
  }

  # Its own Terraform state, and nothing else in S3.
  statement {
    sid       = "TerraformStateBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${local.state_bucket}"]
  }

  statement {
    sid       = "TerraformStateObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${local.state_bucket}/*"]
  }
}

resource "aws_iam_role_policy" "infra_deploy" {
  name   = "${local.name}-tf-deploy"
  role   = aws_iam_role.infra_deploy.id
  policy = data.aws_iam_policy_document.infra_deploy.json
}
