variable "region" {
  description = "AWS region holding the state bucket and the deployment roles."
  type        = string
  default     = "us-east-1"
}

variable "env" {
  description = "Environment name, used as the first part of every resource name. Must match the env used in ../terraform."
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

# The GitHub identity values have no default on purpose. A default that names
# someone else's account would let their workflows assume roles in yours.
# setup/publish-workloads.sh writes them to terraform.tfvars for you.
variable "github_owner" {
  description = "GitHub account or organisation that owns both repositories."
  type        = string
}

variable "github_owner_id" {
  description = "Numeric id of the GitHub account. GitHub embeds this in the OIDC subject claim so that renaming the account cannot carry this trust with it. Find it with: gh api /users/<owner> --jq .id"
  type        = number
}

variable "infra_repo" {
  description = "Repository holding this Terraform. Only workflows in it may assume the infrastructure deployment role."
  type        = string
  default     = "tokenomics-status-board-infra"
}

variable "infra_repo_id" {
  description = "Numeric id of the infrastructure repository, embedded in the OIDC subject claim. Find it with: gh api /repos/<owner>/<repo> --jq .id"
  type        = number
}

variable "app_repo" {
  description = "Repository holding the application. Only workflows in it may assume the application deployment role."
  type        = string
  default     = "tokenomics-status-board-app"
}

variable "app_repo_id" {
  description = "Numeric id of the application repository, embedded in the OIDC subject claim. Find it with: gh api /repos/<owner>/<repo> --jq .id"
  type        = number
}

variable "hosted_zone_name" {
  description = "Public hosted zone the deployment role may write records into. Must match hosted_zone_name in ../terraform."
  type        = string
  default     = "awsf.brightkey.cloud"
}

# GitHub sends a subject claim that starts with this prefix, followed by
# ":ref:refs/heads/main" or ":pull_request". Its format depends on the account
# and repository settings, so read it from GitHub instead of guessing:
#   gh api repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix
# setup/publish-workloads.sh does this for you. Empty means the immutable
# form, repo:<owner>@<owner_id>/<repo>@<repo_id>.
variable "infra_oidc_subject_prefix" {
  description = "Start of the OIDC subject claim for the infrastructure repository. Empty means repo:<owner>@<owner_id>/<repo>@<repo_id>."
  type        = string
  default     = ""

  validation {
    condition     = var.infra_oidc_subject_prefix == "" || startswith(var.infra_oidc_subject_prefix, "repo:")
    error_message = "infra_oidc_subject_prefix must start with repo:, or be empty."
  }
}

variable "app_oidc_subject_prefix" {
  description = "Start of the OIDC subject claim for the application repository. Empty means repo:<owner>@<owner_id>/<repo>@<repo_id>."
  type        = string
  default     = ""

  validation {
    condition     = var.app_oidc_subject_prefix == "" || startswith(var.app_oidc_subject_prefix, "repo:")
    error_message = "app_oidc_subject_prefix must start with repo:, or be empty."
  }
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub Actions OIDC provider. An account can hold only one provider for token.actions.githubusercontent.com. Set false if the account already has it, and this layer uses the existing one."
  type        = bool
  default     = true
}
