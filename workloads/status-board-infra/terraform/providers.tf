provider "aws" {
  region = var.region

  # Applied to every taggable resource in this stack.
  default_tags {
    tags = {
      env        = var.env
      costcenter = var.costcenter
      managed-by = "terraform"
      repo       = var.repo_name
      directory  = "terraform"
    }
  }
}
