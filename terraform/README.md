# terraform

One Terraform root for both environments of the WebbPulse Terraform control
plane. `var.environment` is `staging` or `production` and selects everything
else; resource names use the contract's slug, where `production` becomes `prod`.

## Commands

```bash
terraform fmt -recursive
terraform init -backend=false      # module and provider download, no state
terraform validate
```

Runs happen in HCP Terraform, never locally: workspace `ws-xVqd4ioXLARZhGd4`
on branch `staging` and `ws-u8uD1PZ54bphMXFU` on `main`, working directory
`terraform`, auto-apply off. AWS credentials come from the workspace's dynamic
provider credentials, so a local `terraform plan` has no way to authenticate.

## Layout

| File | What it holds |
| --- | --- |
| `versions.tf` | Terraform and provider constraints, the `cloud {}` block |
| `providers.tf` | The default provider, `us_east_1` for the CloudFront cert, `dns` and `parent_dns` assume-role aliases |
| `variables.tf`, `locals.tf`, `data.tf` | Inputs, the derived names, the caller identity |
| `dynamodb.tf` | The four tables: workspaces, runs, variables, config-versions |
| `s3.tf` | The state bucket and the artifacts bucket, each with its own KMS key |
| `ecr.tf` | `webbpulse-terraform/{workspaces,runs,runner}` |
| `lambda_domains.tf` | The `workspaces` and `runs` functions, their roles and inline policies |
| `apigateway.tf` | The HTTP API, the route keys and the JWT authorizer |
| `identity.tf`, `app_secrets.tf` | The identity platform module and the single JSON `app` secret |
| `vpc.tf`, `ecs.tf`, `runner_logs.tf` | The public-only VPC, the Fargate cluster and the two phase task definitions, the runner log group |
| `step_functions.tf`, `state_machines/run.asl.json` | The per-run state machine |
| `frontend.tf`, `acm.tf`, `route53.tf` | The SPA distribution, the certificates, the staging child zone with its NS delegation, and the alias records |
| `staging_access_gate.tf` | Staging only, the email gate in front of the site and the API |
| `iam_github_actions.tf` | The deploy and CI OIDC roles |
| `monitoring.tf`, `management.tf` | The three aggregate alarms in production, budgets |
| `outputs.tf` | Everything the workflows and the GitHub environment variables read |

## Hostnames

The contract names one hostname per environment. The API needs a custom domain
of its own, so the frontend takes `terraform.webbpulse.com` (staging
`staging.terraform.webbpulse.com`) and the API takes `api.` in front of it,
matching the Portfolio split.

## Two stage bootstrap

Two values are false on a greenfield environment and true afterwards:

- `staging_gate_attach_api_authorizer`. The gate module decides whether to
  create the origin-verify authorizer with `count` on `http_api_id`, and the
  API id is unknown until the API exists, so a first plan with it on fails with
  an invalid count argument.
- `variables_master_key_keep`. The app secret's generated key cannot be read
  back before the secret has a version.

## Staging profile

`var.staging_profile` is `none`, `reduced` or `full`. With `none` the staging
workspace refuses to plan. `reduced` skips the custom domains and the gate;
`full` adds both.
