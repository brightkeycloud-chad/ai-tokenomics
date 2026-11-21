# status-board-infra

Terraform for the Service Status Board. Two layers: `bootstrap/` is applied by
hand once, `terraform/` is applied by GitHub Actions through OIDC.

## Layout

| Path | What lives there |
|---|---|
| `bootstrap/` | GitHub OIDC provider, deployment and agent roles |
| `bootstrap/create-state-bucket.sh` | Creates the shared state bucket, outside Terraform |
| `terraform/network.tf` | VPC, subnets, regional NAT gateway, route tables |
| `terraform/security_groups.tf` | Load balancer and task security groups |
| `terraform/alb.tf` | Load balancer, target group, listeners, DNS record |
| `terraform/ecs.tf` | Cluster, task definition, service, log group |
| `terraform/dynamodb.tf` | The two tables |
| `terraform/ecr.tf` | Image repository and its lifecycle policy |
| `terraform/iam.tf` | Task execution role and task role |

## Working on it

```bash
cd terraform
terraform init -backend=false    # enough for fmt and validate; no AWS needed
terraform fmt -recursive
terraform validate

terraform init -backend-config=backend.hcl -reconfigure
terraform plan
```

Run `fmt` and `validate` before every commit. `plan` needs credentials for the
account.

## Conventions

1. **Name everything `{env}-{service}-{resource}`**, kebab-case and lowercase,
   built from the `local.name` prefix. For example `tokenomics-status-board-alb`.

2. **Tag through `default_tags`.** The provider block applies `env`,
   `costcenter`, `managed-by`, `repo` and `directory` to everything taggable.
   Add only a `Name` tag on individual resources.

3. **Look values up, do not hard-code them.** Availability Zones, the ACM
   certificate, the hosted zone and the account id all come from data sources,
   so the stack moves between regions and survives a certificate renewal.

4. **Describe every variable and output.** Both carry a `description`, and
   variables carry a `validation` block where a wrong value would fail late.

5. **Keep the teardown non-interactive.** Anything that would block
   `terraform destroy` — images in a repository, deletion protection, a
   non-empty bucket — is configured so that it does not.

6. **Least privilege in IAM.** Role management in the deployment policies is
   confined to the `{env}-{service}-*` name prefix. The agent role explicitly
   denies the writes most likely to change the environment, and is refused
   anything else because it is never allowed.

## Things worth knowing

- **Both layers keep state in S3**, in the same bucket under different keys,
  locked with the S3 native lock file. There is no local state file and no
  DynamoDB lock table.

- **The state bucket is not a Terraform resource.** It is created by
  `bootstrap/create-state-bucket.sh`. A layer that manages the bucket holding
  its own state cannot be destroyed cleanly. Do not move it into Terraform.

- **The NAT gateway is regional, in automatic mode.** Do not add
  `availability_zone_address` blocks or `aws_eip` resources to it. Declaring
  the addresses by hand disables automatic expansion across zones and runs
  into a provider ordering bug when the zone count changes.

- **The wildcard certificate covers one label.** `tokenomics.awsf.brightkey.cloud`
  is covered, `tokenomics.demo.awsf.brightkey.cloud` is not. The zone, the
  label and the certificate are variables; nothing else names the domain.

- **`/healthz` must never touch DynamoDB.** The target group health check
  points at it, and the demonstration depends on a failing check meaning the
  load balancer cannot reach the task, and nothing else.

- **Always filter `aws_availability_zones` to `opt-in-not-required`.** In an
  account with Local Zones opted in, their names sort ahead of `us-east-1a`,
  so an unfiltered list silently returns Local Zones and the load balancer
  rejects the subnets as being from "multiple locales".

- **Do not create AWS Config or CloudTrail resources here.** An account has
  one configuration recorder per region, usually managed outside this stack.
  A second one fails the apply.

- **`aws_ecs_service` ignores changes to `task_definition`.** The application
  repository registers new revisions, and Terraform would otherwise roll them
  back on the next apply.
