# WebbPulse-Terraform

Serverless Terraform control plane for WebbPulse. Workspaces hold variables and
configuration versions; a run takes one configuration version through a plan and,
once confirmed, an apply, executed by a Fargate task that Step Functions drives
one phase at a time. The API is FastAPI behind a Lambda Web Adapter, one
container image per domain, with DynamoDB for records, S3 for state and
artifacts, and a React SPA in front. This file is the contract the four slices
share. `terraform/README.md` holds the bootstrap sequence and the workspace
variables, and `TFC-REPLACEMENT.md` at the WebbPulse root holds the replacement
plan and current status.

## Environments

`var.environment` is `staging` or `production` and selects everything else.
Resource names use the environment slug, where `production` becomes `prod`, in
the prefix `webbpulse-terraform-<slug>`. Both accounts are `us-west-2`.

| | Staging | Production |
| --- | --- | --- |
| Branch | `staging` | `main` |
| AWS account | 870550636948 | 897427573432 |
| Slug | `staging` | `prod` |
| Frontend host | `staging.terraform.webbpulse.com` | `terraform.webbpulse.com` |
| API host | `api.staging.terraform.webbpulse.com` | `api.terraform.webbpulse.com` |
| `identity_jwt_mode` | `gate` | `native` |
| Access gate | optional, `var.staging_access_gate` | never |

Custom domains and the gate require `var.staging_profile` of `full` together with
`var.route53_zone_id`. With `reduced` the hostnames fall back to the CloudFront
and HTTP API endpoints, and the CORS origin follows.

## Repository layout

| Path | Slice |
| --- | --- |
| `terraform/` | The single Terraform root for both environments |
| `backend/` | The FastAPI domains, one Lambda container image each |
| `frontend/` | The Vite React SPA on CloudFront and S3 |
| `runner/` | The Fargate task that executes one phase of one run |

Each slice carries its own README with its layout and commands.

## Storage

Two buckets, each with its own KMS key, both created by the platform
`s3-bucket` module.

| Bucket | Holds | Lifecycle |
| --- | --- | --- |
| `webbpulse-terraform-<slug>-state` | Workspace state at `workspaces/<workspace_id>/terraform.tfstate` | Noncurrent versions expire after 365 days, 10 kept |
| `webbpulse-terraform-<slug>-artifacts` | Config tarballs under `configs/`, plan artifacts and phase logs under `runs/` | Both prefixes expire after `var.artifact_retention_days`, default 90 |

The artifacts bucket allows CORS `PUT` from the frontend origin so a
configuration version uploads straight to S3 over a presigned URL. The runner
never holds bucket credentials: every artifact transfer is presigned.

State locking is Terraform's native S3 lockfile, which is why the engine floor is
1.11.

## DynamoDB

Four tables, prefixed `webbpulse-terraform-<slug>-`, with point in time recovery
on and deletion protection in production.

| Table | Key | Index |
| --- | --- | --- |
| `workspaces` | `workspace_id` | `by_name` on `name` |
| `runs` | `run_id` | `by_workspace` on `workspace_id`, range `created_at` |
| `variables` | `workspace_id`, range `key` | none |
| `config-versions` | `config_version_id` | `by_workspace` on `workspace_id`, range `created_at` |

The `runs` table also holds the concurrency semaphore as a single item with
`run_id` of `run-semaphore`, whose `holders` string set the state machine adds to
and removes from. `var.run_concurrency_cap`, default 2, is the size it is
condition checked against.

The `workspaces` domain owns `workspaces`, `variables` and `config-versions` and
reads `runs`; the `runs` domain owns `runs` and reads the other three.

## Backend

Every route is mounted under `/api/v1` and reached through the HTTP API. A person
arrives with a JWT the gateway authorizer verified, an agent with a `wpk_` API
key, and both render as the same claims, so a scope guard cannot tell them apart.
The two runner routes carry no gateway authorizer and are gated in the
application on a run token bound to the run in the path.

| Route | Scope |
| --- | --- |
| `GET /workspaces` | `workspaces:read` |
| `POST /workspaces` | `workspaces:write` |
| `GET /workspaces/{workspace_id}` | `workspaces:read` |
| `PATCH /workspaces/{workspace_id}` | `workspaces:write` |
| `DELETE /workspaces/{workspace_id}` | `workspaces:write` |
| `GET /workspaces/{workspace_id}/variables` | `variables:read` |
| `GET /workspaces/{workspace_id}/variables/{key}` | `variables:read` |
| `PUT /workspaces/{workspace_id}/variables/{key}` | `variables:write` |
| `DELETE /workspaces/{workspace_id}/variables/{key}` | `variables:write` |
| `POST /workspaces/{workspace_id}/config-versions` | `configs:write` |
| `GET /workspaces/{workspace_id}/config-versions` | `configs:read` |
| `GET /workspaces/{workspace_id}/config-versions/{config_version_id}` | `configs:read` |
| `POST /runs` | `runs:write` |
| `GET /runs` | `runs:read` |
| `GET /runs/{run_id}` | `runs:read` |
| `POST /runs/{run_id}/confirm` | `runs:apply` |
| `POST /runs/{run_id}/cancel` | `runs:write` |
| `POST /runs/{run_id}/discard` | `runs:write` |
| `GET /runs/{run_id}/logs` | `runs:read` |
| `GET /runs/{run_id}/bundle` | run token |
| `POST /runs/{run_id}/phase-result` | run token |

`runs:apply` exists so confirming an apply can be granted separately from
creating or cancelling a run. The identity routes under `/api/auth` come from the
shared identity module and are served by the `workspaces` function.

The confirmations queue consumer is mounted outside `/api/v1`, on the Lambda Web
Adapter's pass-through path, so the HTTP API never routes it.

## Runs and Step Functions

A run holds one of ten states.

| State | Meaning |
| --- | --- |
| `pending` | Created, queued behind another run on the workspace |
| `planning` | Plan task running |
| `planned` | Plan finished |
| `awaiting_confirmation` | Plan has changes and is waiting for `runs:apply` |
| `applying` | Apply task running |
| `applied` | Apply finished |
| `planned_and_finished` | Plan only, or a plan with no changes |
| `errored` | A phase or the execution failed |
| `cancelled` | Cancelled |
| `discarded` | Discarded before it applied |

`applied`, `planned_and_finished`, `errored`, `cancelled` and `discarded` are
terminal. Only `awaiting_confirmation` is confirmable; `planned` and
`awaiting_confirmation` are discardable.

One execution per run, named for the run id, started by the runs domain with
`run_id`, `workspace_id`, `plan_only` and `run_token`. Every state writes
`ResultPath` as `null` or into its own key and none sets `OutputPath`, so the
input survives to the apply task, which is what lets both container overrides
read `RUN_TOKEN` from `$.run_token`. The execution input is the only place the
token plaintext is written; the state machine runs with `include_execution_data`
off, so it never reaches the execution log. The states in order:

1. `AcquireSemaphore` adds the run to the `holders` set, condition checked
   against the cap, retrying every 15 seconds up to 240 times.
2. `Plan` runs the plan task definition with `runTask.waitForTaskToken`, timing
   out at `var.plan_timeout_seconds`, default 1800, with a 600 second heartbeat.
3. `PlanOutcome` chooses: a plan only run, or one whose add, change and destroy
   counts are all zero, goes straight to `MarkPlannedAndFinished`; anything else
   goes to `AwaitConfirmation`.
4. `AwaitConfirmation` uses `sqs:sendMessage.waitForTaskToken` to put a
   `run_confirmation_requested` message carrying the run id and the task token on
   the confirmations queue, timing out at `var.confirmation_timeout_seconds`,
   default 86400.
5. `Apply` runs the apply task definition the same way, timing out at
   `var.apply_timeout_seconds`, default 7200.
6. `MarkApplied` or `MarkPlannedAndFinished` then `ReleaseSemaphore`; any failure
   catches to `MarkErrored` then `ReleaseSemaphoreAfterFailure` then `Failed`. A
   failure before the semaphore was taken goes to `MarkErroredWithoutSemaphore`.

A Step Functions task token cannot travel through a DynamoDB integration, so the
confirmation path is indirect. The state machine sends the token to the queue,
the runs domain's confirmations consumer stores it against the run conditionally
on the run still awaiting a confirmation, and `POST /runs/{run_id}/confirm` sends
task success with it. Until the token lands the confirm route answers 409, which
is the only window in which a planned run cannot be confirmed. The event source
mapping runs with a batch size of one and reports batch item failures, so a
malformed or unusable message parks on the dead letter queue rather than losing a
token nobody else holds.

## Runner

A linux/arm64 image on Fargate, one task per phase, launched into the public
subnets of the runner VPC. The task definition supplies `TF_IN_AUTOMATION`,
`ENVIRONMENT`, `AWS_REGION_NAME`, `PHASE` and `RUNNER_LOG_GROUP`; the state
machine's container overrides add `RUN_ID`, `WORKSPACE_ID`, `PHASE`,
`TASK_TOKEN`, `RUN_TOKEN` and `API_BASE_URL`. `RunnerEnv` requires `RUN_ID`,
`PHASE`, `TASK_TOKEN`, `API_BASE_URL`, `RUN_TOKEN` and `RUNNER_LOG_GROUP`.

The runner fetches `GET /api/v1/runs/{run_id}/bundle` with the run token as
bearer. The bundle is the only response in the API carrying decrypted variable
values, which is why it is gated on the run token rather than on scopes. Its
shape is `Bundle` in `runner/app/models.py`, which the backend serves exactly:

| Field | Holds |
| --- | --- |
| `run_id`, `workspace_id` | The run and its workspace |
| `phase` | Derived from the run's status, never taken from the caller |
| `plan_only` | Whether the run stops after the plan |
| `engine`, `engine_version` | `terraform` or `tofu`, and the pinned version |
| `working_directory` | Directory within the configuration |
| `config_url` | Presigned GET for the config tarball |
| `backend` | `bucket`, `key`, `region`, `kms_key_id`, the last holding the key ARN |
| `run_role` | `role_arn`, `external_id` (the workspace id), `session_policy`, `duration_seconds` |
| `terraform_variables`, `environment_variables` | Decrypted values |
| `artifacts` | `plan_put_url`, `plan_json_put_url`, `plan_get_url`, `log_put_url` |

The session policy is read only for a plan and unrestricted for an apply. The
runner models the nested objects and `engine_version` and ignores the other three
top level fields, which state what the API served rather than instructing it.

Presigned URLs live for one hour and all four objects sit under `runs/<run_id>/`
in the artifacts bucket, which is the prefix the bucket's lifecycle rule expires.
The log key is `runs/<run_id>/<phase>.log`, so a plan and an apply keep separate
transcripts. The runner unpacks the tarball refusing members
that escape the working directory, writes the S3 backend override and an auto
loaded tfvars file, assumes the run role with the workspace id as the external id
and the phase session policy, and exports only those credentials to the engine.
It streams the engine's output to the `<run_id>/<phase>` stream in the runner log
group, uploads the plan artifacts and the log over the presigned URLs, posts
`POST /runs/{run_id}/phase-result`, then sends task success with the exit code
and the change counts or task failure. Every line passes through the redactor
first, and the engine's environment is built without the runner's own tokens.

## Run identity

Ids are a prefix plus a ULID, matching `^<prefix>-[0-9A-HJKMNP-TV-Z]{26}$`:
`ws-` for a workspace, `cv-` for a configuration version, `run-` for a run.

A run token is a `wpk_` key the runs domain mints when the run starts, carrying
the `runner` scope, an expiry four hours out, and the run id as its subject. The
plaintext exists once, at mint time. It is not interchangeable with an agent key:
the guard checks the key verifies, carries the `runner` scope, and is bound to
the run in the path, so a token minted for one run cannot read another run's
bundle. Only the key hash is stored on the run row, and neither it nor the stored
confirmation task token is ever rendered to a caller.

## Pins

`webbpulse` and `@webbpulse/*` float to the newest release at build time rather
than being pinned. The backend's floor is `webbpulse>=0.42.0`; the frontend takes
`>=0.13.0 <1` for each `@webbpulse/*` package. Platform modules are `2.25.1` from
the HCP registry, rewritten at cutover. Terraform is `>= 1.11`, and the runner
image pins its own engine versions in `runner/versions.env`.
