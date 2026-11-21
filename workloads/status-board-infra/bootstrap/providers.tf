provider "aws" {
  region = var.region

  default_tags {
    tags = {
      env        = var.env
      costcenter = var.costcenter
      managed-by = "terraform"
      repo       = var.infra_repo
      directory  = "bootstrap"
    }
  }
}
