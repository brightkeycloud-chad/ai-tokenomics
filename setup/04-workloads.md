# Publish and deploy the workloads

The toolkit uses two repositories: the Status Board application and its Terraform infrastructure. Templates for both are in [`workloads/`](../workloads). This guide publishes them to your GitHub account, connects GitHub Actions to your AWS account, and deploys the stack.

## When you need this

| Tool | What you need |
|---|---|
| `bin/run-baseline` and the baseline tasks | Nothing from this guide. The baseline tasks run on local copies of `workloads/`. |
| The CloudWatch logs example (`tokenomics extensions`) | The deployed stack. The agent reads its CloudWatch logs. |
| `bin/claude-local`, `bin/codex-local`, and `tokenomics ollama` | Nothing from this guide. |

You can publish the repositories at any time. **Deploy the stack only before you run the CloudWatch logs example, and tear it down after.** It costs about $0.10 for each hour that it exists.

## Requirements

| Item | Check |
|---|---|
| GitHub CLI, signed in | `gh auth status` |
| AWS CLI with credentials for your account | `aws sts get-caller-identity` |
| Terraform 1.10 or later | `terraform version` |
| A public Route 53 hosted zone | The default is `awsf.brightkey.cloud`. Use a zone in your own account. |
| An ISSUED ACM certificate in `us-east-1` | It must cover `tokenomics.<zone>`. The default lookup expects a certificate whose primary name is the zone, with a wildcard name. |

If your zone is not `awsf.brightkey.cloud`, set `HOSTED_ZONE_NAME` before step 1. If your certificate has a different primary name, set `CERTIFICATE_DOMAIN` before step 3.

## 1. Publish the repositories

From the root of this toolkit repository:

```bash
HOSTED_ZONE_NAME=awsf.brightkey.cloud ./setup/publish-workloads.sh
```

The script:

- Creates two private repositories in your account: `tokenomics-status-board-app` and `tokenomics-status-board-infra`.
- Puts a local clone of each in `~/tokenomics/`.
- Keeps GitHub Actions disabled on both, until step 3 sets their variables.
- Writes `bootstrap/terraform.tfvars` in the infrastructure clone, with your GitHub ids.
- Checks your AWS account for a GitHub OIDC provider. An account can have only one. If one exists, the bootstrap uses it.
- Reads from GitHub the start of the OIDC subject claim for each repository (`sub_claim_prefix`), and writes it to `terraform.tfvars`. The trust policies then accept exactly what GitHub sends. The format depends on account and repository settings: for example `repo:<owner>@<owner_id>/<repo>@<repo_id>` or `repo:<owner>/<repo>`.

To use other names or another directory, set `OWNER`, `APP_REPO`, `INFRA_REPO`, or `DEST`. The script header lists all settings.

## 2. Apply the bootstrap layer (one time)

```bash
cd ~/tokenomics/tokenomics-status-board-infra
./bootstrap/create-state-bucket.sh                  # state bucket, and backend.hcl in both layers
terraform -chdir=bootstrap init -backend-config=backend.hcl
terraform -chdir=bootstrap apply                    # OIDC provider (if necessary) and three IAM roles
```

This layer stays in your account until you remove it. It costs nothing.

## 3. Set the repository variables

From the root of this toolkit repository:

```bash
SUBDOMAIN=tokenomics ./setup/configure-repos.sh
```

The script sets the variables that the workflows read, and enables GitHub Actions:

| Repository | Variables |
|---|---|
| Infrastructure | `AWS_REGION`, `AWS_ROLE_ARN`, `TF_STATE_BUCKET`, `NAME_PREFIX`, `ENV_NAME`, `HOSTED_ZONE_NAME`, `SUBDOMAIN`, and `CERTIFICATE_DOMAIN` if set |
| Application | `AWS_REGION`, `AWS_ROLE_ARN`, `NAME_PREFIX`, `BOARD_URL` |

The script also prints an AWS CLI profile for the read-only agent role. Add it to `~/.aws/config`. The agent uses it in the CloudWatch logs example.

## 4. Check the AWS access

Each repository has a `check-aws-access` workflow. It shows the OIDC subject claim, assumes the deployment role, and makes read-only calls. It deploys nothing and costs nothing.

```bash
OWNER=$(gh api user --jq .login)
gh workflow run check-aws-access.yml --repo "$OWNER/tokenomics-status-board-infra"
gh workflow run check-aws-access.yml --repo "$OWNER/tokenomics-status-board-app"
gh run list --workflow check-aws-access.yml --repo "$OWNER/tokenomics-status-board-infra" --limit 1
```

Expected result:

| Repository | The run shows |
|---|---|
| Infrastructure | The subject claim, the assumed `...-tf-deploy` role, and a plan summary such as `Plan: 37 to add, 0 to change, 0 to destroy.` (when the stack is down) |
| Application | The subject claim, the assumed `...-app-deploy` role, and `ECR login: allowed` |

Run it again whenever a workflow fails with `AccessDenied`.

## 5. Deploy the stack (before the CloudWatch logs example)

From the root of this toolkit repository:

```bash
setup/deploy-stack.sh
```

The script runs with no prompts, in about 7 minutes:

1. It checks that both repositories exist, that their variables are set, and that GitHub Actions is on.
2. It runs the `terraform` workflow in the infrastructure repository and waits for it.
3. It runs the `deploy` workflow in the application repository and waits for it. This builds the image, rolls the service onto it, and loads the sample data.
4. It checks that `/healthz` returns 200 and that `/api/services` has 4 or more services.

If a workflow fails, the script stops and prints the failed steps. To check the setup without starting anything, use `setup/deploy-stack.sh --dry-run`. If the stack is up and you changed only the application, use `--app-only`.

The script is safe to run again. Until the application deployment finishes, the ECS service restarts because it has no image. That is expected on a new stack.

## 6. Tear down the stack (after the CloudWatch logs example)

From the root of this toolkit repository:

```bash
setup/teardown-stack.sh
```

The script runs with no prompts, in about 4 minutes:

1. It runs the `destroy` workflow and waits for it.
2. It deregisters and deletes the ECS task definitions that the application deployments registered. Terraform removes only its own revision. This step uses your own AWS credentials. If `AWS_PROFILE` is set to the read-only agent profile, the script stops.
3. It checks for clusters, load balancers, NAT gateways, tables, ECR repositories, and task definitions with the name prefix. It exits with an error if any remain.

To see what exists now without changing anything, use `setup/teardown-stack.sh --dry-run`. The script refuses to run unless the name prefix starts with `tokenomics-`, so it cannot touch another stack in the same account.

The bootstrap layer and the state bucket stay. They cost nothing or almost nothing. To remove them when you no longer need the stack, see "Tearing it down" in the infrastructure repository README.

## Things that can go wrong

| Problem | What to do |
|---|---|
| A workflow fails at "Check the repository variables" | Run step 3 again. |
| "Assume the deployment role" fails with `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The subject claim does not match the trust policy. Run `check-aws-access` and copy the subject claim from its first step. Compare it with `terraform -chdir=bootstrap output oidc_subjects`. Then see the next three rows. |
| The subject claim has a different format from `oidc_subjects` | Read the prefix that GitHub sends: `gh api repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix`. Put it in `infra_oidc_subject_prefix` or `app_oidc_subject_prefix` in `bootstrap/terraform.tfvars`, and apply the bootstrap again. |
| The subject claim ends with `:environment:...` or has extra parts | The repository uses a custom subject template. Reset it: `gh api -X PUT repos/<owner>/<repo>/actions/oidc/customization/sub -F use_default=true`. |
| The run was on a branch other than `main`, and not in a pull request | That is expected. The roles trust only `main` and pull requests. |
| "Assume the deployment role" fails with `No OpenIDConnect provider found` | The account has no GitHub OIDC provider. Set `create_github_oidc_provider = true` in `bootstrap/terraform.tfvars` and apply the bootstrap again. |
| Terraform cannot find the certificate | The certificate must be ISSUED, in `us-east-1`, with the primary name in `CERTIFICATE_DOMAIN` (or the zone name). |
| The bootstrap apply fails on the OIDC provider | The account already has one. Set `create_github_oidc_provider = false` in `bootstrap/terraform.tfvars` and apply again. |
| The application deployment fails at the ECR login or push | The stack is not complete. Check the terraform run, then run the deploy workflow again. |
| The board returns 503 | The task is not healthy yet, or it has no image. Wait for the deploy workflow, then look at the ECS service events. |
