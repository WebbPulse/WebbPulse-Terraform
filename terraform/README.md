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
| `sqs.tf` | The run confirmations queue the state machine's task token is sent through |
| `frontend.tf`, `acm.tf`, `route53.tf` | The SPA distribution, the certificates, the staging child zone with its NS delegation, and the alias records |
| `staging_access_gate.tf` | Staging only, the email gate in front of the site and the API |
| `iam_github_actions.tf` | The deploy and CI OIDC roles |
| `monitoring.tf`, `management.tf` | The three aggregate alarms in production, budgets |
| `transaction_search.tf` | The X-Ray trace segment destination, the spans log resource policy and the indexing rule |
| `example_run_role.tf` | The run role for the first end to end run, gated on `var.example_workspace_id` |
| `outputs.tf` | Everything the workflows and the GitHub environment variables read |

## Hostnames

The contract names one hostname per environment. The API needs a custom domain
of its own, so the frontend takes `terraform.webbpulse.com` (staging
`staging.terraform.webbpulse.com`) and the API takes `api.` in front of it,
matching the Portfolio split.

## Bootstrap sequence

Lambda resolves an image tag during `CreateFunction`, so the two domain
functions cannot be created before their ECR repositories hold an image.
`var.bootstrap_image_tag` gates them: the empty string resolves the domain map
to empty, and everything downstream of the functions resolves to nothing with
it.

| Run | `bootstrap_image_tag` | What happens |
| --- | --- | --- |
| 1 | `""` (the default) | Everything but the two domain functions: the API with no integrations and no routes, the tables, buckets, VPC, cluster, state machine, gate and roles |
| between | n/a | Merge the backend to `staging`. `deploy-backend` builds and pushes `sha-<head sha>` for both domains and skips the Lambda deploy, because neither function exists yet |
| 2 | `sha-<head sha>` | The two functions, their integrations, routes and permissions, the runs SQS event source and the identity role policies |
| after | unchanged | `deploy-backend` owns the image. Each push deploys by digest through `UpdateFunctionCode` |

Set `bootstrap_image_tag` by hand as a workspace variable on the HCP workspace,
not through the factory. It must be `sha-` followed by a full 40 character
commit sha, which is the tag the image build pushes.

The tag is read only when a function is created. It can expire out of ECR,
which keeps three tagged images, without affecting a running function, so
refresh it to a tag that still exists before any apply that recreates one.

## Transaction Search

Both domain functions export OTLP spans, and X-Ray rejects the export with a 400
until the account's trace segment destination is `CloudWatchLogs`.
`transaction_search.tf` sets that destination, grants `xray.amazonaws.com` the
`logs:PutLogEvents` it needs on `aws/spans` and `/aws/application-signals/data`,
and holds the indexing rule at 1 percent.

X-Ray creates the reserved `aws/spans` log group itself on the first span it
writes, and Terraform cannot pre-create a name beginning with `aws/`. So an
account applies once with `adopt_spans_log_group` false, generates one span, then
sets the workspace variable true and applies again to adopt the group and hold it
at 7 day retention. Neither account has written a span yet, so both start false.

## Staging profile

`var.staging_profile` is `none`, `reduced` or `full`. With `none` the staging
workspace refuses to plan. `reduced` skips the custom domains and the gate;
`full` adds both.

## Variables set on the workspace

Everything else takes its default.

| Variable | Value |
| --- | --- |
| `environment` | `staging` or `production` |
| `staging_profile` | `none`, `reduced` or `full` |
| `bootstrap_image_tag` | `""` on run 1, then `sha-<head sha>`; see the bootstrap sequence |
| `runner_image_tag` | The runner image tag the task definitions point at. Unlike a Lambda image it is not ignored, so a revision follows it |
| `route53_zone_id`, `route53_write_role_arn` | The parent zone and the role that writes into it, both required when `staging_profile` is `full` |
| `staging_access_gate`, `staging_access_users` | Staging only: put the site and API behind the email gate, and who may sign in |
| `identity_jwt_mode` | `off`, `gate` or `native`. Staging uses `gate`, production `native` |
| `adopt_spans_log_group` | `false` until the first span is written, then `true`; see Transaction Search |
| `example_workspace_id` | The `ws-` id of the example workspace. Non-empty creates the example run role for the first end to end run; empty, the default, creates nothing. See `examples/first-run/README.md` |
