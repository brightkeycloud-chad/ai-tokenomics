#!/usr/bin/env bash
#
# Tear down the Status Board stack, with no prompts:
#   1. Run the destroy workflow in the infrastructure repository.
#   2. Deregister and delete the ECS task definitions that the application
#      deployments registered. Terraform removes only its own revision.
#   3. Check that nothing that costs money remains.
#
# It takes about 4 minutes. Step 2 uses your own AWS credentials, not the
# read-only agent role. The bootstrap layer and the state bucket stay; they
# cost nothing or almost nothing.
#
# Safe to run again: when the stack is already down, the destroy changes
# nothing, and the checks report what remains.
#
#   setup/teardown-stack.sh             tear down and check
#   setup/teardown-stack.sh --dry-run   show what exists now; change nothing
#
# It refuses to run unless the name prefix starts with "tokenomics-", so it
# cannot touch another stack in the same account.
#
# Settings: OWNER, APP_REPO, INFRA_REPO (see setup/lib-stack.sh).

source "$(dirname "$0")/lib-stack.sh"

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) fail "unknown option: $arg (use --dry-run)" ;;
  esac
done

say "Check the setup"
need_repo_vars "$INFRA" AWS_ROLE_ARN TF_STATE_BUCKET
PREFIX=$(repo_var "$INFRA" NAME_PREFIX)
PREFIX="${PREFIX:-tokenomics-status-board}"
case "$PREFIX" in
  tokenomics-*) ;;
  *) fail "name prefix '$PREFIX' does not start with tokenomics-; refusing to touch it" ;;
esac
REGION=$(repo_var "$INFRA" AWS_REGION)
REGION="${REGION:-us-east-1}"
export AWS_REGION="$REGION"

caller=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null) \
  || fail "no AWS credentials. Step 2 needs your own credentials."
case "$caller" in
  *assumed-role/*-agent/*) fail "AWS credentials are the read-only agent role ($caller). Unset AWS_PROFILE and run again." ;;
esac
echo "   infrastructure: $INFRA"
echo "   name prefix:    $PREFIX"
echo "   region:         $REGION"
echo "   AWS identity:   $caller"

# Resources that cost money, or that a complete teardown removes.
remaining() {
  local found=0 out
  check() {
    local label="$1"; shift
    out=$("$@" 2>/dev/null | tr '\t' '\n' | grep -v '^None$' | grep . || true)
    if [ -n "$out" ]; then
      echo "   $label:"; sed 's/^/     /' <<<"$out"; found=1
    else
      echo "   $label: none"
    fi
  }
  check "ECS clusters" aws ecs list-clusters \
    --query "clusterArns[?contains(@,'$PREFIX')]" --output text
  check "Load balancers" aws elbv2 describe-load-balancers \
    --query "LoadBalancers[?contains(LoadBalancerName,'$PREFIX')].LoadBalancerName" --output text
  check "NAT gateways" aws ec2 describe-nat-gateways \
    --filter Name=state,Values=available,pending "Name=tag:Name,Values=$PREFIX*" \
    --query "NatGateways[].NatGatewayId" --output text
  check "DynamoDB tables" aws dynamodb list-tables \
    --query "TableNames[?contains(@,'$PREFIX')]" --output text
  check "ECR repositories" aws ecr describe-repositories \
    --query "repositories[?contains(repositoryName,'$PREFIX')].repositoryName" --output text
  check "Task definitions (ACTIVE)" aws ecs list-task-definitions \
    --family-prefix "$PREFIX" --status ACTIVE --query 'taskDefinitionArns[]' --output text
  return "$found"
}

if [ "$DRY_RUN" = 1 ]; then
  say "Dry run: what exists now"
  if remaining; then echo; echo "   Nothing to tear down."; fi
  exit 0
fi

say "1. Destroy the stack (destroy workflow, about 4 minutes)"
run_and_wait "$INFRA" destroy.yml -f confirm=destroy

say "2. Remove the task definitions of the application deployments"
arns=$(aws ecs list-task-definitions --family-prefix "$PREFIX" --status ACTIVE \
  --query 'taskDefinitionArns[]' --output text | tr '\t' '\n' | grep '^arn:' || true)
for arn in $arns; do
  aws ecs deregister-task-definition --task-definition "$arn" >/dev/null
  echo "   deregistered ${arn##*/}"
done
arns=$(aws ecs list-task-definitions --family-prefix "$PREFIX" --status INACTIVE \
  --query 'taskDefinitionArns[]' --output text | tr '\t' '\n' | grep '^arn:' || true)
for arn in $arns; do
  aws ecs delete-task-definitions --task-definitions "$arn" >/dev/null
  echo "   deleted ${arn##*/}"
done
[ -n "$arns" ] || echo "   no inactive revisions to delete"

say "3. Check that nothing remains"
if remaining; then
  say "The stack is down ($(elapsed)). Nothing that costs money remains."
  echo "   The bootstrap roles and the state bucket stay for the next deployment."
else
  fail "some resources remain (see above). Run this script again; if they stay, look at them before you delete anything by hand."
fi
