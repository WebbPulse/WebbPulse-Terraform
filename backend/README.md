# WebbPulse Terraform backend

The control plane's API: workspaces, variables, configuration versions and runs.
FastAPI behind a Lambda Web Adapter, one container image per domain, DynamoDB for
state and Step Functions for each run.

## Layout

```
app/
  common/
    composition/   settings, the domain map and both composition roots
    core/          auth, middleware, logging, the variable cipher
    db/            table names and repositories
  domains/
    workspaces/    workspaces, variables, config versions
    runs/          runs, the runner's bundle, artifact uploads and phase results
      consumers/   the queue consumers and the one route they share
e2e/               the deployed-stage suite, extending webbpulse.e2e
tests/
  common/          the auth chain and every route's scope guard
  domains/<name>/  one directory per domain, which is how CI shards
  entrypoints/     both composition roots, and that they cannot drift
```

Two composition roots are built from the same `wiring.DOMAINS` map, so they
cannot drift. `app/common/composition/app.py` mounts every domain in one process
for local work and the suite. `app/domains/<name>/entrypoint.py` is what that
domain's Lambda runs, and carries only its own routes.

## Commands

`webbpulse` is served from CodeArtifact, so point uv at it before the first
`uv sync` or `uv lock`. The token lasts twelve hours.

```bash
export UV_INDEX_CODEARTIFACT_USERNAME=aws
export UV_INDEX_CODEARTIFACT_PASSWORD="$(aws codeartifact get-authorization-token \
  --domain webbpulse --domain-owner 432410731887 --region us-west-2 \
  --query authorizationToken --output text)"
```

```bash
uv sync                      # install, including dev dependencies
uv run ruff check            # lint
uv run ruff format           # format
uv run pyright               # type check app and tests
uv run pytest                # the whole suite, moto backed
uv run pytest tests/domains/runs   # one domain
```

Local DynamoDB, for running the app outside the suite:

```bash
docker compose up -d dynamodb-local
uv run python scripts/create_local_tables.py
uv run uvicorn app.common.composition.app:app --reload --port 8000
```

Each domain also runs as its own container, the way it is deployed:

```bash
docker compose --profile domains up --build
```

## End-to-end

`e2e/` extends the shared `webbpulse.e2e` plugin rather than forking it:
`test_shared.py` collects the seven shared groups, `conftest.py` supplies the
product hooks, and `test_product_flows.py` carries the run lifecycle.

```bash
uv sync --group e2e
uv run pytest e2e --no-cov -n auto --dist loadgroup
```

The suite reads the `E2E_*` variables, which the reusable workflows set from the
`staging` Environment. `e2e.yml` runs the full suite after every deploy;
`e2e-local.yml` runs it on a stack built from source on each pull request.

Two product facts the shared plugin cannot know:

- Write scopes come from `is_admin`, so the `ephemeral_user` fixture is
  overridden to create its user with that attribute.
- The SPA carries no `data-testid` attributes, so the login form hook supplies
  CSS locators.

## Auth

A person arrives with a JWT the API Gateway authorizer has already verified. An
agent arrives with a `wpk_` API key. Both render as the same claims object, so a
route guarded by `require_scopes` cannot tell them apart. The scopes are
`workspaces:{read,write}`, `variables:{read,write}`, `configs:{read,write}`,
`runs:{read,write,apply}`.

The runner is separate. Starting a run mints a `wpk_` key scoped `runner`, bound
to that run and expiring after four hours, and only that token opens
`GET /runs/{id}/bundle`, `POST /runs/{id}/artifact-uploads` and
`POST /runs/{id}/phase-result`. The bundle carries decrypted sensitive
variables, so no human scope reaches it, and every terminal transition revokes
the token.

The bundle mints only the plan's read URL. An upload's presigned PUT signs the
exact `Content-Length` it will receive, which is unknown when the bundle is
built, so the runner posts each artifact's byte count to
`POST /runs/{id}/artifact-uploads` as `{artifact, size_bytes}` and gets back
`{url, headers, expires_in}` to send verbatim. A size above that artifact's
ceiling is a 413, and the log's key comes from the run's stored phase, so a
plan-phase runner cannot target the apply transcript.

## Identity

The JWTs above are issued by this same backend. The workspaces function mounts
the `webbpulse.identity` router, which carries its own `/api/auth` prefix, so the
issuer and the product API are one deployment. The router mounts only when
`IDENTITY_ISSUER` is set, and `app/common/identity/package_glue.py` imports the
package inside function bodies, so the runs image never carries any of it.

Accounts live in the `users` table, which is this repository's own. The identity
module's ten tables are named from `IDENTITY_TABLE_PREFIX`, which Terraform sets
to `local.prefix`. That variable is read rather than derived because the stack
slugs production to `prod` while `ENVIRONMENT` is the word `production`.

`ControlPlaneIdentityHooks` refuses a disabled or unverified account with one
message for both, so a caller cannot use the refusal to tell which addresses
exist, and maps `is_admin` onto an `admin` entry in the token's `roles` claim.

It also stamps the token's `scope` claim, space joined, which is what
`require_scopes` reads once `coerce_claims` splits it. An admin holds every scope
in `ALL_SCOPES`; anyone else holds the read scopes only, so a signed-in non-admin
can see the control plane without changing it. `runner` is in neither, because it
is not in `ALL_SCOPES`. The refresh flow re-derives claims through the same hook,
so a rotated token carries the scopes a fresh sign-in does.

### Creating the first account

`IDENTITY_REGISTRATION_ENABLED` is `false` in every deployed environment, so
there is no self-service sign-up. `scripts/create_user.py` creates or updates one
verified admin and its password credential. It is re-runnable, which is also how
a password is rotated, and it prints nothing secret.

The password comes from `CONTROL_PLANE_USER_PASSWORD`, never from an argument,
so it stays out of the shell history and the process list. The `--environment`
flag must match `ENVIRONMENT`, which is what stops a staging shell writing into
production.

```bash
AWS_PROFILE=WebbPulse-Terraform-Staging/AgentToolkit \
AWS_REGION=us-west-2 \
ENVIRONMENT=staging \
IDENTITY_TABLE_PREFIX=webbpulse-terraform-staging \
USERS_TABLE=webbpulse-terraform-staging-users \
CONTROL_PLANE_USER_PASSWORD='...' \
  uv run python scripts/create_user.py --environment staging tyler@webbpulse.com
```

`IDENTITY_TABLE_PREFIX` is passed explicitly here because a shell is not the
Lambda and carries none of the function's environment. It is `local.prefix`:
`webbpulse-terraform-staging` in staging and `webbpulse-terraform-prod` in
production.

## HCL variables

A terraform variable can set `hcl`, which makes the runner write its value into a
native `zz_webbpulse.auto.tfvars` as a bare `key = expression` assignment rather
than into the JSON tfvars file, so the engine parses it. That is the only way a
`list` or `map` typed input variable can be given a value. It is refused on an
`env` variable, whose value is a string to the process with nothing to parse it.
The value is structurally checked at write time by `app/domains/workspaces/hcl.py`,
which rules out an unterminated string, heredoc or comment and unbalanced
brackets without adding an HCL parser dependency; the engine stays the authority
on meaning. A sensitive variable can still be HCL: it is sealed like any other
value, the check runs before sealing and never echoes the value, and the runner
registers the expression with the redactor.

## Sensitive variables

A variable marked sensitive is sealed app side with AES-256-GCM under a key
derived by HKDF from the app secret's `variables_master_key`, following the same
envelope pattern as TOTP. The encryption context binds the ciphertext to one
workspace and key, so a row copied to another key will not decrypt. The API never
returns a sensitive value; the run bundle is the only reader.

## The run role

A workspace's run role is optional at create and attached afterwards, because the
role cannot exist first: its trust policy names the workspace id as the external
id, so the id has to be handed out before the role can be written. Every workspace
response carries `run_role_setup` with the runner task roles to trust
(`principal_arn`, and `principal_arns` for every phase), the external id and the
role name, which is `<prefix>-workspace-<ulid>` and has to stay inside the runner's
AssumeRole grant.

`POST /workspaces/{id}/run-role/check` assumes the role with the workspace id as
the external id and a fifteen minute session, then calls GetCallerIdentity on the
temporary credentials. It answers `{connected, account_id, error}` and stamps
`run_role_checked_at` and `run_role_account_id` on a success, clearing both on a
failure and on any PATCH that changes the ARN. A workspace with no ARN is a 400
carrying `RUN_ROLE_MISSING`; creating a run against one is a 409 with the same
code. The credentials never reach a response or a log.

## Runs

Runs are serial per workspace: a run created while another is active is stored
`pending` with `queued_behind` set, and the previous run's terminal transition
promotes the next one. Two concurrent executions would otherwise contend on the
S3 state lock.

A run's phase comes from its stored status, never from the caller, because the
plan phase gets a read-only IAM session policy and the apply phase an
unrestricted one.

Cancelling a run releases the concurrency semaphore itself, because
`StopExecution` skips the state machine's own release states, and creating a run
prunes holders whose runs are finished or gone, so a stuck semaphore is fixed by
starting any run.

A phase task that never starts fails its run promptly rather than waiting out the
Plan or Apply state's six hundred second heartbeat. An EventBridge rule on the
runner cluster's ECS `Task State Change` events with `stopCode` `TaskFailedToStart`
feeds `webbpulse-terraform-<env>-run-task-failures`, and
`app/domains/runs/consumers/task_failures.py` reads the run id and task token back
out of the task's own container environment overrides and sends `SendTaskFailure`,
so the execution takes its existing `MarkErrored` and `ReleaseSemaphoreAfterFailure`
path within seconds.

## The confirmations queue

A Step Functions DynamoDB integration cannot carry a task token, so the state
machine sends `{"kind": "run_confirmation_requested", "run_id", "task_token"}`
to `webbpulse-terraform-<env>-run-confirmations` instead. The runs function
consumes it through an event source mapping with a batch size of one and
`ReportBatchItemFailures`, which makes the Lambda Web Adapter post the batch to
`AWS_LWA_PASS_THROUGH_PATH`. The adapter posts every queue invocation to that one
path, so `app/domains/runs/consumers/dispatch.py` mounts the single route with
`webbpulse.events` and hands each record to the consumer its `kind` names;
`confirmations.py` stores the token, conditionally on the run still awaiting a
confirmation.

The route mounts at the root rather than under `/api/v1`, and the HTTP API never
lists it, so the only way to reach it is the adapter's pass-through. A record
that cannot be stored is returned as a batch item failure, which retries the
message and eventually parks it rather than losing a token an execution is
blocked on.
