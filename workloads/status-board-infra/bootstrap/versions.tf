terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.66"
    }
  }

  # The bucket is created by ./create-state-bucket.sh before this layer is
  # first initialised. It is not managed by Terraform, so that destroying this
  # layer does not remove the bucket holding its own state.
  #
  # The bucket and the region depend on your account. The script writes them
  # to backend.hcl, so initialise with:
  #   terraform init -backend-config=backend.hcl
  backend "s3" {
    key          = "status-board/bootstrap.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}
