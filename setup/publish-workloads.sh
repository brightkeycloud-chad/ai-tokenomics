#!/usr/bin/env bash
#
# Publish the two course workloads as GitHub repositories in your account.
#
# GitHub Actions runs only from the root of a repository, so the workloads in
# this course repository are templates. This script copies each one to a local
# directory, makes it a git repository, creates the GitHub repository, and
# pushes it. It then writes bootstrap/terraform.tfvars in the infrastructure
# copy, with the GitHub ids that the OIDC trust policies need.
#
# GitHub Actions stays disabled on the new repositories until
# configure-repos.sh has set their variables, so the first push does not start
# a workflow that cannot succeed.
#
# Settings (environment variables):
#   OWNER            GitHub user or organization   default: the signed-in gh user
#   APP_REPO         application repository name   default: tokenomics-status-board-app
#   INFRA_REPO       infrastructure repository     default: tokenomics-status-board-infra
#   DEST             local parent directory        default: $HOME/tokenomics
#   VISIBILITY       private or public             default: private
#   HOSTED_ZONE_NAME Route 53 zone for the board   default: awsf.brightkey.cloud
#
# Safe to run again: a repository that already exists on GitHub is skipped,
# and an existing terraform.tfvars is not overwritten.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

for tool in gh git jq rsync; do
  command -v "$tool" >/dev/null || { echo "error: $tool is not installed" >&2; exit 1; }
done
gh auth status >/dev/null 2>&1 || { echo "error: gh is not signed in; run: gh auth login" >&2; exit 1; }

OWNER="${OWNER:-$(gh api user --jq .login)}"
APP_REPO="${APP_REPO:-tokenomics-status-board-app}"
INFRA_REPO="${INFRA_REPO:-tokenomics-status-board-infra}"
DEST="${DEST:-$HOME/tokenomics}"
VISIBILITY="${VISIBILITY:-private}"
HOSTED_ZONE_NAME="${HOSTED_ZONE_NAME:-awsf.brightkey.cloud}"

case "$VISIBILITY" in
  private|public) ;;
  *) echo "error: VISIBILITY must be private or public, got '$VISIBILITY'" >&2; exit 1 ;;
esac

echo "owner      : $OWNER"
echo "repos      : $APP_REPO, $INFRA_REPO ($VISIBILITY)"
echo "local copy : $DEST"
echo

mkdir -p "$DEST"

publish() {
  local source="$1" name="$2" dir="$DEST/$2"

  if gh repo view "$OWNER/$name" >/dev/null 2>&1; then
    echo "skip   $OWNER/$name already exists on GitHub"
    return
  fi
  if [ -e "$dir" ]; then
    echo "error: $dir exists but $OWNER/$name does not. Move the directory, or set DEST." >&2
    exit 1
  fi

  echo "create $OWNER/$name from workloads/$source"
  rsync -a --exclude .terraform --exclude backend.hcl --exclude '*.tfvars' \
    --exclude __pycache__ --exclude venv "$ROOT/workloads/$source/" "$dir/"

  git -C "$dir" init --quiet -b main
  git -C "$dir" add -A
  git -C "$dir" commit --quiet -m "Initial commit from the ai-tokenomics course workloads"

  gh repo create "$OWNER/$name" "--$VISIBILITY" \
    --description "Service Status Board ($source) for the Tokenomics course" >/dev/null
  # Keep workflows off until configure-repos.sh sets the repository variables.
  gh api --silent -X PUT "repos/$OWNER/$name/actions/permissions" -F enabled=false

  git -C "$dir" remote add origin "https://github.com/$OWNER/$name.git"
  git -C "$dir" push --quiet -u origin main
  echo "       pushed to https://github.com/$OWNER/$name (Actions disabled for now)"
}

publish status-board-app "$APP_REPO"
publish status-board-infra "$INFRA_REPO"

# An account can hold only one GitHub Actions OIDC provider. If AWS credentials
# are available, check for one, so that bootstrap uses it instead of failing.
CREATE_OIDC=true
if command -v aws >/dev/null && aws sts get-caller-identity >/dev/null 2>&1; then
  existing=$(aws iam list-open-id-connect-providers \
    --query "OpenIDConnectProviderList[?ends_with(Arn, 'token.actions.githubusercontent.com')].Arn" \
    --output text)
  if [ -n "$existing" ]; then
    CREATE_OIDC=false
    echo
    echo "found an existing GitHub OIDC provider: bootstrap will use it"
  fi
else
  echo
  echo "note: no AWS credentials, so the OIDC provider check was skipped (create_github_oidc_provider = true)"
fi

# The OIDC subject claim format depends on account and repository settings.
# Read the prefix that GitHub will send, so the trust policy matches it exactly.
subject_prefix() {
  local repo="$1" info
  info=$(gh api "repos/$OWNER/$repo/actions/oidc/customization/sub")
  if [ "$(jq -r .use_default <<<"$info")" != "true" ]; then
    echo "error: $OWNER/$repo uses a custom OIDC subject template ($info)." >&2
    echo "       The course trust policy expects the default template. Reset it with:" >&2
    echo "       gh api -X PUT repos/$OWNER/$repo/actions/oidc/customization/sub -F use_default=true" >&2
    exit 1
  fi
  jq -r '.sub_claim_prefix // ""' <<<"$info"
}
INFRA_SUBJECT=$(subject_prefix "$INFRA_REPO")
APP_SUBJECT=$(subject_prefix "$APP_REPO")

TFVARS="$DEST/$INFRA_REPO/bootstrap/terraform.tfvars"
if [ -e "$TFVARS" ]; then
  echo
  echo "keep   $TFVARS (already exists)"
else
  cat > "$TFVARS" <<EOF
# Written by setup/publish-workloads.sh. Not committed.
github_owner    = "$OWNER"
github_owner_id = $(gh api "users/$OWNER" --jq .id)

infra_repo    = "$INFRA_REPO"
infra_repo_id = $(gh api "repos/$OWNER/$INFRA_REPO" --jq .id)

app_repo    = "$APP_REPO"
app_repo_id = $(gh api "repos/$OWNER/$APP_REPO" --jq .id)

# What GitHub puts at the start of each OIDC subject claim (read from the API).
infra_oidc_subject_prefix = "$INFRA_SUBJECT"
app_oidc_subject_prefix   = "$APP_SUBJECT"

hosted_zone_name            = "$HOSTED_ZONE_NAME"
create_github_oidc_provider = $CREATE_OIDC
EOF
  echo
  echo "wrote  $TFVARS"
fi

cat <<EOF

Next steps (see setup/04-workloads.md):
  cd "$DEST/$INFRA_REPO"
  ./bootstrap/create-state-bucket.sh
  terraform -chdir=bootstrap init -backend-config=backend.hcl
  terraform -chdir=bootstrap apply
  "$ROOT/setup/configure-repos.sh"
EOF
