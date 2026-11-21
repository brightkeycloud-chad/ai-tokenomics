#!/usr/bin/env bash
#
# Creates the S3 bucket that holds Terraform state for both layers.
#
# This bucket is deliberately not managed by Terraform. A layer that manages
# the bucket holding its own state cannot be destroyed cleanly, because the
# bucket disappears while Terraform is still writing to it. Creating it here
# keeps `terraform destroy` working on both layers.
#
# Safe to run repeatedly. Existing buckets are left alone.
#
# It also writes backend.hcl in ../bootstrap and ../terraform, so that local
# runs can initialise with: terraform init -backend-config=backend.hcl
# Those files hold the bucket name and region only, and are not committed.
#
# Settings (environment variables):
#   ENV_NAME    first part of every name     default: tokenomics
#   SERVICE     second part of every name    default: status-board
#   AWS_REGION  region of the bucket         default: us-east-1
#   INFRA_REPO  value of the repo tag        default: tokenomics-status-board-infra

set -euo pipefail

ENV_NAME="${ENV_NAME:-tokenomics}"
SERVICE="${SERVICE:-status-board}"
REGION="${AWS_REGION:-us-east-1}"
INFRA_REPO="${INFRA_REPO:-tokenomics-status-board-infra}"
HERE="$(cd "$(dirname "$0")" && pwd)"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${ENV_NAME}-${SERVICE}-tfstate-${ACCOUNT_ID}"

echo "account : ${ACCOUNT_ID}"
echo "region  : ${REGION}"
echo "bucket  : ${BUCKET}"

if aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  echo "bucket already exists, leaving it alone"
else
  echo "creating bucket"
  # us-east-1 rejects a LocationConstraint; every other region requires one.
  if [ "${REGION}" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}"
  else
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
      --create-bucket-configuration "LocationConstraint=${REGION}"
  fi
  aws s3api wait bucket-exists --bucket "${BUCKET}"
fi

echo "applying versioning, encryption and public access block"

aws s3api put-bucket-versioning \
  --bucket "${BUCKET}" \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption \
  --bucket "${BUCKET}" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

aws s3api put-public-access-block \
  --bucket "${BUCKET}" \
  --public-access-block-configuration \
  'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'

aws s3api put-bucket-tagging \
  --bucket "${BUCKET}" \
  --tagging "TagSet=[
    {Key=env,Value=${ENV_NAME}},
    {Key=costcenter,Value=${ENV_NAME}},
    {Key=managed-by,Value=script},
    {Key=repo,Value=${INFRA_REPO}},
    {Key=directory,Value=bootstrap}
  ]"

for layer in "${HERE}" "${HERE}/../terraform"; do
  printf 'bucket = "%s"\nregion = "%s"\n' "${BUCKET}" "${REGION}" > "${layer}/backend.hcl"
  echo "wrote $(cd "${layer}" && pwd)/backend.hcl"
done

echo
echo "ready: s3://${BUCKET}"
