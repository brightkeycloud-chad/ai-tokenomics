#!/usr/bin/env bash
#
# Set the GitHub repository variables that the workload workflows read, from
# the outputs of the infrastructure bootstrap layer. Then enable GitHub Actions
# on both repositories.
#
# Run it after `terraform apply` in bootstrap/. Safe to run again: each
# variable is overwritten with the current value.
#
# Settings (environment variables):
#   INFRA_DIR           local infrastructure repository   default: $HOME/tokenomics/tokenomics-status-board-infra
#   SUBDOMAIN           label in front of the zone        default: tokenomics
#   CERTIFICATE_DOMAIN  primary name of the certificate   default: empty (the zone name)

set -euo pipefail

for tool in gh terraform; do
  command -v "$tool" >/dev/null || { echo "error: $tool is not installed" >&2; exit 1; }
done
gh auth status >/dev/null 2>&1 || { echo "error: gh is not signed in; run: gh auth login" >&2; exit 1; }

INFRA_DIR="${INFRA_DIR:-$HOME/tokenomics/tokenomics-status-board-infra}"
SUBDOMAIN="${SUBDOMAIN:-tokenomics}"
CERTIFICATE_DOMAIN="${CERTIFICATE_DOMAIN:-}"

if [ ! -d "$INFRA_DIR/bootstrap" ]; then
  echo "error: $INFRA_DIR/bootstrap not found. Set INFRA_DIR to your infrastructure repository." >&2
  exit 1
fi

out() {
  terraform -chdir="$INFRA_DIR/bootstrap" output -raw "$1" 2>/dev/null || {
    echo "error: bootstrap output '$1' is missing. Run terraform apply in $INFRA_DIR/bootstrap first." >&2
    exit 1
  }
}

INFRA=$(out infra_repo)
APP=$(out app_repo)
REGION=$(out region)
PREFIX=$(out name_prefix)
ENV_NAME=$(out env)
ZONE=$(out hosted_zone_name)
BUCKET=$(out state_bucket)
INFRA_ROLE=$(out infra_deploy_role_arn)
APP_ROLE=$(out app_deploy_role_arn)
AGENT_ROLE=$(out agent_role_arn)
BOARD_URL="https://$SUBDOMAIN.$ZONE"

setvar() {
  local repo="$1" name="$2" value="$3"
  gh variable set "$name" --repo "$repo" --body "$value"
  printf '  %-20s %s\n' "$name" "$value"
}

echo "$INFRA"
setvar "$INFRA" AWS_REGION "$REGION"
setvar "$INFRA" AWS_ROLE_ARN "$INFRA_ROLE"
setvar "$INFRA" TF_STATE_BUCKET "$BUCKET"
setvar "$INFRA" NAME_PREFIX "$PREFIX"
setvar "$INFRA" ENV_NAME "$ENV_NAME"
setvar "$INFRA" HOSTED_ZONE_NAME "$ZONE"
setvar "$INFRA" SUBDOMAIN "$SUBDOMAIN"
if [ -n "$CERTIFICATE_DOMAIN" ]; then
  setvar "$INFRA" CERTIFICATE_DOMAIN "$CERTIFICATE_DOMAIN"
fi

echo "$APP"
setvar "$APP" AWS_REGION "$REGION"
setvar "$APP" AWS_ROLE_ARN "$APP_ROLE"
setvar "$APP" NAME_PREFIX "$PREFIX"
setvar "$APP" BOARD_URL "$BOARD_URL"

for repo in "$INFRA" "$APP"; do
  gh api --silent -X PUT "repos/$repo/actions/permissions" -F enabled=true
done
echo
echo "GitHub Actions enabled on both repositories."

cat <<EOF

Next steps (see setup/04-workloads.md):
  gh workflow run terraform.yml --repo $INFRA     # about 4 minutes
  gh workflow run deploy.yml --repo $APP          # after the stack is up, about 3 minutes
  curl -s -o /dev/null -w '%{http_code}\n' $BOARD_URL/healthz   # expect 200

For the CloudWatch logs example, add this read-only profile to ~/.aws/config:

  [profile tokenomics-agent]
  role_arn       = $AGENT_ROLE
  source_profile = default
  region         = $REGION
EOF
