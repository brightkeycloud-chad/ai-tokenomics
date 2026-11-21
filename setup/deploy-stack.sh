#!/usr/bin/env bash
#
# Deploy the Status Board stack for the CloudWatch logs example, with no prompts:
#   1. Run the terraform workflow in the infrastructure repository.
#   2. Run the deploy workflow in the application repository (image and seed data).
#   3. Check that the board answers.
#
# It takes about 7 minutes. The stack costs about $0.10 for each hour that it
# exists. Tear it down with setup/teardown-stack.sh.
#
# Safe to run again: Terraform changes nothing that is already correct, and the
# application deployment rolls the service onto the same code.
#
#   setup/deploy-stack.sh             deploy and check
#   setup/deploy-stack.sh --dry-run   check the setup only; start nothing
#   setup/deploy-stack.sh --app-only  only the application deployment (the stack is up)
#
# Settings: OWNER, APP_REPO, INFRA_REPO (see setup/lib-stack.sh).

source "$(dirname "$0")/lib-stack.sh"

DRY_RUN=0
APP_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --app-only) APP_ONLY=1 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) fail "unknown option: $arg (use --dry-run or --app-only)" ;;
  esac
done

say "Check the setup"
need_repo_vars "$INFRA" AWS_ROLE_ARN TF_STATE_BUCKET
need_repo_vars "$APP" AWS_ROLE_ARN BOARD_URL
BOARD_URL=$(repo_var "$APP" BOARD_URL)
BOARD_URL="${BOARD_URL%/}"
echo "   infrastructure: $INFRA"
echo "   application:    $APP"
echo "   board:          $BOARD_URL"

if [ "$DRY_RUN" = 1 ]; then
  say "Dry run: the setup is ready. Nothing was started."
  exit 0
fi

if [ "$APP_ONLY" = 0 ]; then
  say "1. The stack (terraform workflow, about 4 minutes)"
  run_and_wait "$INFRA" terraform.yml
fi

say "2. The application and the seed data (deploy workflow, about 3 minutes)"
run_and_wait "$APP" deploy.yml

say "3. Check the board"
code=""
for attempt in $(seq 1 24); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BOARD_URL/healthz" || true)
  [ "$code" = 200 ] && break
  sleep 5
done
[ "$code" = 200 ] || fail "$BOARD_URL/healthz returned $code, expected 200"
echo "   /healthz: 200"

count=$(curl -s "$BOARD_URL/api/services" \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
[ "$count" -ge 4 ] || fail "$BOARD_URL/api/services has $count services, expected 4 or more"
echo "   /api/services: $count services"

say "The stack is up ($(elapsed))"
cat <<EOF
   Board: $BOARD_URL
   Cost:  about \$0.10 for each hour, until you tear it down.
   Tear down after the demo:  setup/teardown-stack.sh
EOF
