terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.66"
    }
  }

  # State lives in the bucket created by ../bootstrap/create-state-bucket.sh.
  # Locking uses the S3 native lock file, so no DynamoDB lock table is needed.
  #
  # The bucket and the region are not written here, because they depend on
  # your account. Supply them when you initialise:
  #   terraform init -backend-config=backend.hcl            (local, file written by the script)
  #   terraform init -backend-config="bucket=..." -backend-config="region=..."   (workflow)
  backend "s3" {
    key          = "status-board/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}
