# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## Project Overview

The serverless Terraform control plane for WebbPulse, replacing HCP Terraform.
Workspaces, variables, configuration versions and runs, with each run executed by
a Fargate task rather than by a hosted runner.

**Stack:** FastAPI (Python 3.13) backend deployed as Lambda container images
behind an HTTP API, DynamoDB for control plane state, S3 for Terraform state with
native locking (`use_lockfile = true`), one Step Functions execution per run and
one Fargate runner task per plan and per apply. The frontend is a React 19
TypeScript SPA on the `@webbpulse/*` packages. Infrastructure is Terraform
(`terraform/`), applied by the plane itself.

**Environments:** `staging` serves `staging.terraform.webbpulse.com` with the API
at `api.staging.terraform.webbpulse.com`. `main` serves `terraform.webbpulse.com`
and `api.terraform.webbpulse.com`.

Design and runbooks for the replacement live in `TFC-REPLACEMENT.md` at the
WebbPulse root. Read it there rather than restating it here. Work tracking is the
Standupless TF project "HCP Terraform replacement".

---

## Commands

### Backend (`backend/`)

The private `webbpulse` package is published only to org CodeArtifact, so any
shell that runs `uv sync` or `uv lock` needs a token. It lasts twelve hours, and
the profile is the Artifacts account, not the staging one:

```bash
export UV_INDEX_CODEARTIFACT_USERNAME=aws
export UV_INDEX_CODEARTIFACT_PASSWORD="$(AWS_PROFILE=WebbPulse-Artifacts/AgentToolkit \
  aws codeartifact get-authorization-token \
  --domain webbpulse --domain-owner 432410731887 --region us-west-2 \
  --query authorizationToken --output text)"
```

```bash
uv sync                            # install, including the dev group
uv run ruff format . && uv run ruff check .
uv run pyright                     # app and tests
uv run pytest                      # the whole suite, moto backed
uv run pytest tests/domains/runs   # one domain, which is how CI shards
uv run bandit -r app -ll
```

Tests run on moto in memory, so nothing has to be running. For the app outside
the suite:

```bash
docker compose up -d dynamodb-local
uv run python scripts/create_local_tables.py
uv run uvicorn app.common.composition.app:app --reload --port 8000
```

One `backend/Dockerfile` builds one image per domain, selected by the `DOMAIN`
build argument (`workspaces`, `runs`, `github` or `registry`). The base image lives in the Artifacts
account, so log in to that ECR first, and the build reads the CodeArtifact token
as a Docker secret:

```bash
aws ecr get-login-password --region us-west-2 \
  | docker login --username AWS --password-stdin \
    432410731887.dkr.ecr.us-west-2.amazonaws.com

export CODEARTIFACT_AUTH_TOKEN="$UV_INDEX_CODEARTIFACT_PASSWORD"
docker compose --profile domains up --build
```

### Frontend (`frontend/`)

`@webbpulse/*` comes from CodeArtifact too. `frontend/.npmrc` points the scope at
that repository and deliberately carries no token, so fetch one before the first
install:

```bash
AWS_PROFILE=WebbPulse-Artifacts/AgentToolkit AWS_REGION=us-west-2 \
  aws codeartifact login --tool npm \
    --domain webbpulse --domain-owner 432410731887 \
    --repository npm --namespace @webbpulse
```

```bash
npm install
npm run dev:local        # :5173, proxying /api to localhost:8000
npm run dev:remote-api   # against api.staging.terraform.webbpulse.com
npm run lint && npm run format:check && npm run test:run && npm run build
```

There is no `npm run dev` and no `type-check` script. `npm run build` is
`tsc -b` then the Vite build, which is where types are checked.

### Runner (`runner/`)

No `webbpulse` dependency, so no CodeArtifact token.

```bash
uv sync
uv run ruff check app tests && uv run ruff format app tests
uv run pyright              # strict, including tests
uv run pytest
docker buildx build --platform linux/arm64 -t webbpulse-terraform-runner:local .
```

The pinned Terraform and OpenTofu versions and their SHA256 sums live in
`runner/versions.env`, which the Dockerfile reads.

### Terraform (`terraform/`)

```bash
terraform fmt -recursive
terraform init -backend=false
terraform validate
```

Applies happen on the plane itself (`WebbPulse-Terraform-staging` on `staging`,
`WebbPulse-Terraform` on `main`), never locally. If the plane cannot apply its
own fix, `ops/break-glass/break_glass.py` plans and applies against the same S3
state; see `terraform/README.md`.

---

## Architecture

### Two composition roots

- **Root A**, `app/common/composition/app.py`, builds every domain into one
  process. It is what local development and the test suite run against.
- **Root B**, `app/domains/<name>/entrypoint.py`, builds one application per
  deployed function and carries only that domain's routes.

Both go through `build_domain_app` in `app/common/composition/wiring.py`, so the
middleware stack and the route surface are identical locally, in the suite and in
each function. Composition is `include_router`, never `mount`, and everything
mounts under `/api/v1`.

### The domain registry

`app/common/composition/wiring.py` is the only place a domain is declared. The
`DOMAINS` map carries `workspaces`, `runs`, `github` and `registry`. Adding one is a package under
`app/domains/`, one entry in the map, a two line entrypoint and the matching
Terraform entry. Loaders are lazy, so importing the registry imports no endpoint
module and each image carries only its own code.

`tests/entrypoints/test_entrypoints.py` is what holds the invariant: that each
domain has an entrypoint, that one function does not serve another's routes, and
that Root A is exactly the union of the Root B applications.

### Auth

A person arrives with a JWT the API Gateway authorizer has already verified, an
agent with a `wpk_` API key that the gateway authorizer (the access gate in both
environments, or the `lambda` mode authorizer where the gate is off) passes through by prefix and
`claims_or_api_key` verifies in process, with `live_scopes` reloading the owner. Both render as the same claims object,
so a route guarded by `require_scopes` cannot tell them apart. Every product
route in `terraform/apigateway.tf` carries `require_identity_jwt`; only the two
runner routes, `POST /github/webhooks` (a webhook signature), the registry protocol
under `/v1/modules` and `/v1/providers` (a `wpk_` key only), `POST /v1/oauth/token`
(a PKCE code) and the anonymous identity documents do not.
The scopes are `workspaces:{read,write}`, `variables:{read,write}`,
`configs:{read,write}`, `runs:{read,write,apply}`, `state:download` and
`registry:{read,write}`. A key's stored scopes are intersected per request with
its owner's current ones (`key_owner_scopes`, so every domain function reads the
`users` table), and a new key expires in 90 days by default, 365 at most for a
dated expiry, or never with `no_expiry: true` (`expires_at: null`).

State history and metadata require `workspaces:read`. Raw state downloads also
require `state:download`, granted to admin sessions and explicitly delegated agent
keys, never ordinary read-only sessions. Existing keys need that scope granted
through a newly minted key. Download URLs expire after 60 seconds, pin one S3
version, and return `Cache-Control: no-store` on both the API and S3 responses.
History cursors are bound to the workspace's state key and invalid ones return 400; clients
must follow `next_page_token` even on an empty page caused by delete markers.

The runner is separate. A run token is a `wpk_` key scoped `runner`, bound to
one run and expiring after four hours, and only it opens the bundle, the
artifact uploads and the phase result. The bundle carries decrypted sensitive
variables, so no human scope reaches it, and every terminal transition revokes
the token. Every run key revoke goes through `revoke_run_key`, which also stamps
`purge_at`, the `api-keys` table's TTL, one day out, so dead run keys leave the
table; agent keys never get it. `scripts/backfill_run_key_ttl.py` stamps rows
revoked before that. The runner gets its token from `POST /runs/{id}/runner-token` (no
authorizer) by sending the headers of an STS `GetCallerIdentity` it signed with
its task role, with the run id in the signed `x-webbpulse-run-id` header
(`app/domains/runs/runner_tokens.py`). The route replays that to regional STS,
requires a runner task role session, whose session name is the task id, then
requires `ecs:DescribeTasks` on `RUNNER_CLUSTER_ARN` to show that task still
running with this `RUN_ID` and a `PHASE` matching the run's status. It mints a
token, swaps `run_token_hash` conditionally on that status and revokes the one
it replaced; every refusal is the same 401 and logs `runs.runner_token.refused`.
The runner routes use `RunnerRoute`, so a malformed request answers that
401 rather than a 422 naming the schema, unless it carries the run's own token.
So the token never has to travel in the Step Functions execution input.

### The run role

A workspace's run role is optional at create, since its trust policy names the
workspace id as the external id and so the id has to exist first. Every workspace
response carries `run_role_setup` (the principal to trust, the external id and the
derived role name). The one principal a run role trusts is the control plane's
vending role, `<prefix>-run-credentials`; no runner task role can assume a run
role and the task roles hold no `sts:AssumeRole`. `GET /workspaces/{id}/run-role/check` reports
`{connected, status, account_id, error, run_id, checked_at}` from the vending
AssumeRole outcome in the newest run on the current ARN, never from a separate STS call,
so a role no run has tried is `unverified` and a plan only run is the check. The
POST gives the same answer and stamps `run_role_checked_at` and
`run_role_account_id`. A run created against a workspace with no run role is a
409 carrying `RUN_ROLE_MISSING`.

Credentials are vended per phase by the runs function when it serves the bundle
(`app/domains/runs/vending.py`): the runs function role assumes the vending role,
which assumes the workspace's run role (external id = workspace id; a plan passes
`ReadOnlyAccess` as its session policy ARN plus an inline document granting
`secretsmanager:GetSecretValue` and `kms:Decrypt` via Secrets Manager or SSM, so
refresh and ephemeral reads of secrets and SecureString parameters work, an apply
none; the session name is
`<run>-<phase>@<workspace name>`, cut to 64) and the state role
`<prefix>-run-state`, narrowed by `session_policy.state_policy` to
`workspaces/<id>/` (a plan may write only `*.tflock`). Both sessions last
`run_credentials_duration_seconds` (default and chained-role cap 3600, floor 900).
The staging e2e run role (`terraform/e2e_run_role.tf`) trusts only sessions named
`run-*@e2e-*`, the suite's own workspaces, plus the durable ids in
`e2e_run_role_workspace_ids` (the `first-run` workspace).
`POST /runs/{id}/credentials` (run token, `{phase}`) vends the same pair again, refused
once the run has left that phase or its task token no longer resolves.
A refused run role is a 409 `RUN_ROLE_ASSUME_FAILED` on the bundle, which the
runner reports as `AssumeRoleFailed`.

`ReadOnlyAccess` holds no `sts:AssumeRole`, so a plan whose providers assume
roles elsewhere (Route 53 writers in a zone account, say) is denied. A workspace
lists exact reader role ARNs in `plan_assume_role_arns` (at most 10, 140
characters each so the inline document fits STS's 2,048 characters, no wildcards,
null clears), and a plan's inline document then also carries an `sts:AssumeRole`
statement on exactly those ARNs; an apply
is unchanged. The runner exports `TF_VAR_webbpulse_run_phase` (`plan` or `apply`,
set after workspace variables so they cannot override it). A config that assumes
roles declares it and selects readers in plan, writers in apply:

```hcl
variable "webbpulse_run_phase" {
  type      = string
  default   = "apply"
  ephemeral = true
}

provider "aws" {
  alias = "dns"
  assume_role {
    role_arn = var.webbpulse_run_phase == "plan" ? local.dns_reader : local.dns_writer
  }
}
```

`ephemeral = true` is required, not style: a saved plan stores every other
variable's value, so the apply of `plan.tfplan` would keep `plan` and pick the
reader. The default `apply` keeps the config working on HCP Terraform and
locally, and an undeclared `TF_VAR_webbpulse_run_phase` is ignored by both
engines. Needs Terraform 1.10 or OpenTofu 1.11 and later. The session policy is
the boundary, the variable only picks which listed role to ask for.

`POST /workspaces/{id}/run-role/quick-setup` takes an optional account id, saves the
derived ARN when one is given and returns an AWS CloudFormation quick create link. The template
(`app/domains/workspaces/quick_setup.py`) trusts only the vending role with
the workspace id as the external id, and is stored content addressed under
`templates/run-role/` in the artifacts bucket, served by presigned GET.

Quick setup needs no account id when `AWS_CONNECT_TOPIC_ARN` is set. The link
carries a one-time connect token (stored only as its SHA-256, one hour, single
use) and the stack's `Connection` custom resource publishes to that SNS topic.
The runs function consumes it (`consumers/aws_connect.py`): the account comes
from the stack ARN, the role must be the workspace's derived one, a valid create
stages it with the same rules as an edit and starts a plan only verification
run, and a bad or expired token answers FAILED so the stack rolls back. A delete
always answers SUCCESS and forgets the role only if that stack still provides
it. The workspace's `aws_connection` records what the UI shows, and its
`verification` (`pending`, `verified` or `failed` with `verification_error`) is
settled when a run on that role ends: a clean finish verifies it, and an error or
a cancel fails it. A staged role is only switched to once its run finished its
plan. The `Connection` resource carries `TrustVersion` (`aws_connect.TRUST_VERSION`,
"2" since vending), recorded on the connection; a Quick setup role whose
connection lacks the current version is `run_role_reconnect_required`, and the UI
asks for the stack to be deleted and connected again, or updated in place.

A workspace may also carry `plan_role_arn`, a read only role a plan assumes for
its provider keys; the plan still assumes the run role first (900 seconds, keys
discarded) so the run role check keeps its evidence, and applies ignore it.
Quick setup creates it by default (`plan_role`, `PlanRoleName` parameter) at
`role/<run role prefix>plan/plan-<ulid>`, a path because the run role name is
already 64 characters; the existing `<prefix>-workspace-*` vending grant covers
it. It is reported as `PlanRoleArn`, kept on the connection, staged and promoted
with its run role, and dropped with the stack or a hand picked run role. Reader
roles in `plan_assume_role_arns` must trust the plan role.

### Runs

Runs are serial per workspace. A run created while another is active is stored
`pending` with `queued_behind` set, and the previous run's terminal transition
promotes it, because two concurrent executions would contend on the S3 state
lock. The state machine writes `errored`, `applied` and
`planned_and_finished` straight to the table, so the runs stream also delivers
any terminal row without `finished_at` to `consumers/endings.py`, which settles it
as `finish_run` would: revoke the token, record the run role, settle the
connection's verification and promote the queue. A run's phase comes from its stored status, never from the caller: the plan
phase gets a read-only IAM session policy and the apply phase an unrestricted
one.

A Step Functions DynamoDB integration cannot carry a task token, so a
confirmation is sent to the `run-confirmations` SQS queue instead and consumed by
the runs function through an event source mapping. That route mounts at the root
rather than under `/api/v1`, and the HTTP API never lists it, so the only way to
reach it is the Lambda Web Adapter's pass-through path.

### Module registry

The `registry` domain speaks Terraform's module registry protocol. The SPA serves
`/.well-known/terraform.json`, the only anonymous path, pointing `modules.v1` at
`<api host>/v1/modules/`. `GET /v1/modules/{ns}/{name}/{provider}/versions` and
`.../{version}/download` sit past the gate with no authorizer and require a
`wpk_` key holding `registry:read`, which Terraform sends from
`TF_TOKEN_<host>`. The download is a 204 whose `X-Terraform-Get` is a five minute
presigned GET ending `.tar.gz`.

Runs read the registry like HCP's injected `TF_TOKEN`. Each bundle carries
`registry` (`app/domains/runs/registry_credentials.py`): a one hour `wpk_` key
scoped `runner:registry` with the run as its subject, for the SPA host. The run
keeps only its hash in `registry_token_hash`; a newer bundle revokes the previous
key and the run's ending revokes the last. The protocol router accepts it by
scope and tenant, and every other route refuses it since a run has no user
scopes. The runner sets it as `TF_TOKEN_<host>` for `init` alone and registers it
with the redactor.

A configuration that uses the WebbPulse provider (the Platform factory) gets HCP's run
scoped API token the same way. An admin who passed step-up sets the workspace's
`run_api_token_scopes` (a subset of `workspaces:{read,write}`, `variables:{read,write}`
and `registry:{read,write}`, PATCH only, null or `[]` clears), and each bundle then
carries `api` (`app/domains/runs/api_credentials.py`): a `wpk_` key of kind `run_api`
with the run as its subject and the workspace in its metadata, lasting its phase's
timeout. Its scopes are the workspace's current grant intersected with the grant it
was minted under, read on every request (`key_owner_scopes`), so the registry function
reads `workspaces` too. The run keeps its hash in `api_token_hash`, and a newer bundle
or the run's ending revokes it. Behind the access gate the runs function reads the
gate's `x-origin-verify` from SSM (`ORIGIN_VERIFY_PARAMETER`) into the bundle; the
runner task role never holds that read. The runner exports `WEBBPULSE_TF_HOST`,
`WEBBPULSE_TF_TOKEN` and `WEBBPULSE_TF_ORIGIN_VERIFY` to every subcommand, after
workspace variables, and redacts the token and the gate value. The provider block must
leave `host`, `token` and `origin_verify` unset, since its config wins over the
environment.

Publishing follows HCP's tag based "Publish module from VCS". `POST
/api/v1/registry/modules` (`registry:write`) connects a module to a repository the
GitHub App is installed on, resolved like a workspace's `vcs_repo` (422
`VCS_REPO_NOT_INSTALLED`, 409 `GITHUB_NOT_CONFIGURED` with no App). The namespace is
the owner; the name and provider come from the body or from
`terraform-<provider>-<name>`. The module is the repository root, with no
subdirectory. `GET` and `DELETE
/api/v1/registry/modules/{ns}/{name}/{provider}` read and remove a module, the
delete taking every version row and tarball with it.

A push of a `vX.Y.Z` or `X.Y.Z` tag (prerelease suffix allowed) reaches the webhook
route, which sends a `module_tag` message to `registry-ingest`, never
`github-webhooks`. `app/domains/registry/consumers/tags.py` claims the version row
`pending` for every module connected to the repository id, fetches the tagged
commit's archive through the installation token (`app/common/github/archive.py`,
codeload only), repacks it with the module at the root (plain files and
directories, no `.git`, `.terraform`, state or links, a root `.tf` required, at most
100 MB) into `registry/modules/...` and marks it `published`, or `failed` with the
reason. A published version is immutable, so a redelivery or a retag is skipped
without a fetch; a failed or pending one is retried by a new tag push. A GitHub
fault raises so SQS retries. Other tags are ignored and deleting a tag unpublishes
nothing.

Connecting also imports the repository's existing tags, unless the body sets
`import_tags: false`, and `POST .../{ns}/{name}/{provider}/resync`
(`registry:write`, 202) does it again, like HCP's resync. That covers tags pushed
while nothing was connected and GitHub sending no push event when more than three
tags are pushed at once. Both queue a `module_sync` message on `registry-ingest`;
`consumers/sync.py` lists tags through the installation token (`list_tags` in
`app/common/github/archive.py`, at most 10 pages of 100), keeps the semver ones
(`v` preferred over a bare duplicate), skips versions published or failed at the
same commit, and queues up to 100 of the newest as `module_tag` messages carrying
a `module` key, which the tag consumer honours by publishing for that module only.
So a tag reaching both paths publishes once. A sync that cannot be queued at
connect is logged and the module still connects; the resync answers 503
`REGISTRY_SYNC_UNAVAILABLE`. The registry function may send to its own queue.

The module page reads `GET .../{ns}/{name}/{provider}/versions/{version}`
(`registry:read`): the version row plus `docs`, the readme, inputs, outputs,
provider requirements, resources and each `modules/<name>` submodule, read with
`python-hcl2` by `app/domains/registry/docs.py` (bounded file sizes and counts; a
file that does not parse lands in `parse_errors`). The tag consumer stores them at
`<version>.docs.json` beside the tarball, best effort; a missing document or one
older than `docs.DOCS_SCHEMA` is extracted from the tarball on the first view.

In Root A the consumer route is shadowed by the runs consumer at the same
pass-through path, so tests call `route_record` directly.

### Provider registry

`providers.v1` points at `<api host>/v1/providers/`: `.../{ns}/{type}/versions` and
`.../{version}/download/{os}/{arch}`, with the same `registry:read` or runner registry
key as modules. `POST /api/v1/registry/providers` connects a
`terraform-provider-<type>` repository (the namespace is the owner) and imports its
releases; `.../resync` does it again, and a `release` webhook (published, a `vX.Y.Z`
tag) publishes one. `app/domains/registry/providers.py` reads the GoReleaser registry
layout from the release assets (`_manifest.json`, `_SHA256SUMS`, `_SHA256SUMS.sig`),
verifies the detached signature against the SSM public key
`/<prefix>/provider-signing/public-key` (`openpgp.py`), checks each zip's sum, and
stores everything under `registry/providers/`. The download answer carries presigned
URLs and that key as `signing_keys.gpg_public_keys`.

Publishing needs no App: the provider repo's release workflow, run by
`workflow_dispatch` with a tag and an environment, signs with that environment's key
and its OIDC role (`<prefix>-provider-release`, which may write only
`registry/provider-uploads/webbpulse/webbpulse/*`) uploads the same files to
`registry/provider-uploads/<ns>/<type>/<version>/<run>/`, then `upload.json`
(`repository`, `tag`, `actor`, `run_url`) last. EventBridge queues that object on
`registry-ingest` as `provider_upload`; `handle_upload` checks the folder against
`upload.json`, creates the provider row without an App binding if none exists (a
later connect adopts it), runs the same verification and deletes the folder once the
version is settled.

### Terraform login

`login.v1` in the discovery document makes `terraform login <SPA host>` work like
HCP's. The CLI opens `/oauth/authorize` (the SPA's approve page) with its PKCE
challenge and a loopback redirect on ports 10000 to 10010; approving calls `POST
/api/v1/oauth/authorizations` (a person only, behind step-up) for a code stored in
the identity module's `authorization-codes` table, and the CLI exchanges it at `POST
/v1/oauth/token` for a `wpk_` key named `terraform login`, valid 90 days, carrying the
read, config and plan scopes the person holds (`terraform_login.LOGIN_SCOPES`), never
`runs:apply` or `state:download`.

`wp-tf login` is the agent path, with no long-lived key. It runs the OAuth device
grant from the identity package (`device_grant_enabled`, client `wp-tf`) at
`<api>/api/auth/device/*`: the approval page sends a signed-out or stale (over ten
minutes) browser to the SPA's `/sign-in?returnTo=...`, whose guest guard hands it
back once signed in (`deviceHandOff` in `returnPath.ts`). The access token has the
audience `<issuer>/device`, which the gate's authorizer accepts beside the API
audience, and `claims_or_api_key` checks its grant is still live
(`device_grant_liveness`), so a revoke ends it within seconds. The default scopes
leave out `runs:apply`, `state:download` and `admin`, which must be named. The
session lives in the OS keyring and refreshes itself; `backend/e2e/test_device_login.py`
drives the whole flow on staging.

### VCS ingest

The VCS bridge's ingest half. A workspace binds itself to a repository with
`vcs_repo`; there are no per repository roles and no repository map. Connecting a
repository (create or PATCH) resolves it through the GitHub App from the `app`
secret the workspaces function already reads: the App JWT finds the installation
and its token lists the repositories, which records `vcs_repository_id`,
`vcs_installation_id` and the canonical name and defaults `tracked_branch` to the
default branch. A repository the App cannot see is 422 `VCS_REPO_NOT_INSTALLED`,
and a GitHub failure is 502/503 `GITHUB_UNAVAILABLE`. A rename keeps the binding
and a new repository under the old name does not inherit it. `working_directory`
is normalized to a clean relative path, `tracked_branch` must be a valid git branch
name, and trigger patterns are trimmed.

The webhook consumer (below) writes an ingest record to `vcs-uploads` (four day
TTL, longer than the webhook age window) and the tarball to `ingest/<upload_id>.tar.gz`. S3 Object Created on `ingest/`
goes through EventBridge to the
`vcs-ingest` queue as `config_ingested`, and `app/domains/runs/consumers/ingest.py`
reads the record by the upload id in the key, never the object's metadata. For
each bound workspace it applies the branch filter (a push needs `tracked_branch`),
`speculative_plans` (for pull requests), and `trigger_patterns` as recursive globs
against `.webbpulse/changed-paths.txt` (empty patterns mean
`<working_directory>/**`, and a missing list or `*` matches everything).
`file_triggers_enabled: false` is HCP's "Always trigger runs" and skips the path
filter. It copies the tarball to a config version and creates a run sourced `vcs_push` (normal) or
`vcs_pr` (plan only) with a `vcs` block and a `vcs` actor. The config version and
run ids are derived from the upload and the workspace, so a redelivered message or
a second S3 event for the same object creates nothing new. A newer upload from the
same source cancels that source's pending runs and discards one awaiting
confirmation; a late older upload starts nothing. A workspace with no run role is
skipped.

### VCS webhooks

GitHub posts to `POST /api/v1/github/webhooks` (no
authorizer); `WebhookSignatureMiddleware` (`app/common/github/webhooks.py`), the
outermost layer of the github function, checks `X-Hub-Signature-256` against the
app secret's `GITHUB_WEBHOOK_SECRET` and answers 401 before routing. A branch push
or a same-repository pull request (opened, synchronize, reopened) is queued on
`github-webhooks` and a semantic version tag push on `registry-ingest` (above);
forks, other tags and other events are acknowledged and dropped.
GitHub signs no time, so a delivery whose event (`repository.pushed_at`,
`pull_request.updated_at` or `release.published_at`) is older than
`MAX_DELIVERY_AGE` (three days, GitHub's redelivery window) or missing is
acknowledged and dropped; inside the window the delivery keyed dedupe makes a replay a no-op.
`app/domains/runs/consumers/webhooks.py` resolves the commit (a pull request waits
for GitHub's merge commit and uses it, reporting on the head), takes the changed
paths from the push payload or `/pulls/{n}/files`, fetches the tarball with an
installation token and writes the same record and `ingest/` object, repacked with
`.webbpulse/changed-paths.txt`. A delivery no workspace would run on is reported
"No runs needed" without a fetch, except a push to a branch no bound workspace
tracks, which posts no check since the pull request delivery owns its head commit. The upload id derives from the delivery id.

A new App's manifest subscribes to `push` and `pull_request` and points its hook
at `API_BASE_URL`. For an existing App, the admin `POST /api/v1/github/app/webhook`
sets the hook URL and secret through `PATCH /app/hook/config` with the App JWT.
GitHub has no API for the Active flag or the event subscriptions, so those are
set on the App's settings page.

### VCS run reporting

The runs table streams (filtered to `vcs_*` runs) to the runs function, and
`app/domains/runs/consumers/reports.py` reports each status change through the App
(`app/domains/runs/reporting.py`): a `webbpulse-terraform/<workspace>` check run
(external id = run id), a `webbpulse-terraform` aggregate check for the commit, and
one marker-found comment per pull request. Checks and comments are found again on
GitHub; the only writes back are the commit message and, for a push, the pull
request it was merged from (`vcs.pull_request`). A push reports on the signed
commit once the branch contains it, a pull request on its unsigned head only once
it is a parent of the signed merge commit and a commit of that pull request. A
held push run is `action_required`; GitHub never reopens a completed check, so a
confirmation creates new checks of the same names and the held ones are left
completed. Reporting never raises, so it cannot fail or retry a run.

### HCL variables

A terraform variable can set `hcl`, which makes the runner write its value into a
native `zz_webbpulse.auto.tfvars` as `key = (<value>)` rather than into the JSON
tfvars file, so the engine parses it. That is the only way a `list` or `map`
typed input variable can be given a value. It is refused on an `env` variable,
whose value is a string to the process with nothing to parse it. The value is
checked at write time by `app/domains/workspaces/hcl.py` without adding an HCL
parser dependency. The check is also the injection boundary: its scanner mirrors
the engine's string, heredoc, comment and interpolation rules, so a value whose
brackets balance outside them cannot close the wrapping parenthesis and add an
attribute. The engine stays the authority on meaning, so a function call passes
the check and fails the run. A sensitive variable can still be HCL: it is sealed
like any other value, the check runs before sealing and never echoes the value,
and the runner registers the expression and its string and heredoc literals with
the redactor.

### Sensitive variables

A variable marked sensitive is sealed app side with AES-256-GCM under a key
derived by HKDF from the app secret's `variables_master_key`. The encryption
context binds the ciphertext to one workspace and key, so a row copied to another
key will not decrypt. The API never returns a sensitive value; the run bundle is
the only reader.

### Runner protocol

Step Functions starts the runner with `runTask.waitForTaskToken`. It exchanges
its task identity for the run token (`app/identity.py`), failing the phase if
the exchange is refused (no `RUN_TOKEN` override is read), then fetches the
bundle, which carries the vended run role keys (`aws_credentials`) and state keys
(`backend.credentials`), unpacks the config tarball in the fixed
`<tmp>/webbpulse-run/config` (with `TF_DATA_DIR=.terraform`, so `path.module` and
`filename` arguments never change between runs), writes the S3 backend
override (`workspace_key_prefix = "workspaces/<id>/env"`) and the auto loaded
tfvars files, gives the providers the run role keys (`webbpulse-run`, set as
`AWS_PROFILE`) and the S3 backend the state keys (`webbpulse-state`), both as
`credential_process` profiles printing JSON files with an `Expiration` five minutes
early (`app/credential_files.py`). `app/refresher.py` rewrites those files ten
minutes before expiry from the credentials route, and the SDK rereads them, so
phases outlive the one hour chained-role cap; static `AWS_*` keys are stripped from
the engine's environment since they would win and never rotate. It runs the engine (`terraform` or
`tofu`, from the bundle), streams redacted output to CloudWatch Logs, uploads its
artifacts to presigned URLs (a plan with changes that is not plan only also uploads
its working directory as `workdir`, without provider binaries, the backend record,
the plan or the runner's tfvars and override; the apply bundle's
`artifacts.workdir_get_url` restores it in place of the config, so installed modules
and plan generated files such as `archive_file` zips are there, as on HCP Terraform) and reports through `POST /runs/{id}/phase-result`
(an `error_name` for a failure). The runner holds no Step Functions permission:
`phase_tasks.report` resolves the task token from the exchanged task's own ECS
overrides (`runner_task_id`) and sends the success or failure, and the task stop
consumer fails a phase whose runner stopped without reporting. The task role holds
only its log stream, so the container credential endpoint gives the engine
nothing; the engine's environment is also built without the runner's own tokens
or any `AWS_CONTAINER_*` variable. Every line passes through `app.logs.Redactor` first.
Once it holds the token the runner beats `POST /runs/{id}/heartbeat` every minute
(`HEARTBEAT_INTERVAL_SECONDS`), which the runs Lambda turns into
SendTaskHeartbeat; a refused beat or SIGTERM sends the engine SIGINT and fails the
phase as `PhaseInterrupted`. The plan and apply states carry `HeartbeatSeconds`
(`task_heartbeat_seconds`, 20 minutes) and `TimeoutSeconds` (`plan_timeout_seconds`
2 hours, `apply_timeout_seconds` 4 hours, capped at the run token's lifetime); a
silent or over budget runner is stopped by the state machine and the run errors
with a message saying which.

Google and Azure get HCP style workload identity. The control plane is an OIDC issuer
(`terraform/oidc_issuer.tf`, `oidc.<stage host>`, anonymous discovery and JWKS from a
KMS RSA key only the runs function may sign with). A workspace setting
`TFC_GCP_PROVIDER_AUTH` (with `TFC_GCP_WORKLOAD_PROVIDER_NAME`, optionally
`TFC_GCP_RUN_SERVICE_ACCOUNT_EMAIL`) or `TFC_AZURE_PROVIDER_AUTH` (with
`TFC_AZURE_RUN_CLIENT_ID`) gets a one hour RS256 token per cloud in the bundle and on
each refresh (`vending.mint_workload_identity`, `sub`
`workspace:<id>:run_phase:<phase>`); a flag without its setting is a 409
`WORKLOAD_IDENTITY_MISCONFIGURED`, reported as `WorkloadIdentityMisconfigured`. The
runner (`app/workload_identity.py`) writes owner only token files, an
`external_account` credential for `GOOGLE_APPLICATION_CREDENTIALS`, and
`ARM_USE_OIDC`, `ARM_OIDC_TOKEN_FILE_PATH` and `ARM_CLIENT_ID` for Azure, and redacts
the tokens. `backend/e2e/test_workload_identity.py` verifies both inside a real plan.

---

## Conventions

- No code comments. Docstrings on every module, class and function, saying why
  rather than restating the code.
- Fill gaps in the shared packages upstream, in `webbpulse-python`,
  `webbpulse-typescript` or `terraform-aws-platform-modules`, never with a
  product-local workaround.
- Shared packages float to the newest release at build time. An exact pin is the
  explicit exception and should say why.
- User-facing copy says "software engineer", never "developer". No em dashes and
  no tagline language.
- Route guards: a spinner while `isLoading` only, a redirect on `!isAuthenticated`,
  and a guest guard redirects only once `!isBusy`. Never unmount a form on `isBusy`:
  it drops the MFA ticket the password leg returns.
- Use `tyler@webbpulse.com` for management addresses.
- Nothing is clicked in the console. Infrastructure changes go through Terraform.

---

## Branching and deploys

```
feature/* ──PR──▶ staging ──PR──▶ main
                     │              │
                     ▼              ▼
           AWS 870550636948   AWS 897427573432
              (staging)          (production)
```

- Branch new work from `staging`, not `main`. PR into `staging`. Releasing is a
  PR from `staging` into `main`; that PR is the release boundary.
- Never commit directly to `main` or `staging`. Never force-push either. Stacked
  PRs bottom out on `staging`.
- Use a merge commit, not a squash, for a `staging` into `main` release PR. A
  squash produces phantom conflicts on the next release.
- Hotfixes branch from `main` and PR into `main`, then are immediately
  back-merged `main` to `staging`. Skipping the back-merge is how the branches
  silently diverge.
- Both accounts are `us-west-2`. `staging` carries the same rules as `main`
  because a workspace bound to that branch assumes an IAM role in a real AWS
  account: the branch is a credential, not a scratch space.

**Protection.** `main` and `staging` are covered by repository rulesets (pull
request required, force-push and deletion blocked, bypassable only by the
repository admin). Rulesets and environments are owned by the
`WebbPulse-Platform` factory, not by this repository.

## Deploys

Every commit to `staging` or `main` deploys, so treat a commit as a release. The
deploys are path scoped per slice: a commit touching `backend/` rebuilds only the
affected domains, one touching `frontend/` redeploys the SPA, one touching
`runner/` rebuilds the runner image, and `terraform/` is applied by the plane
from the branch (production after a `wp-tf confirm`). `all-checks-passed` is the required check.

**Bootstrapping a fresh environment takes two applies.** Lambda resolves an image
tag during `CreateFunction`, so the domain functions cannot exist before their
ECR repositories hold an image. The first apply runs with
`bootstrap_image_tag = ""`, which builds everything but the two functions. The
backend then deploys and pushes `sha-<sha>` for both domains, and the second
apply runs with `bootstrap_image_tag` set by hand as a workspace variable on the
plane workspace to that tag. After that the deploy workflow owns the image and each
push updates the function by digest. See `terraform/README.md` for the full
sequence and the workspace variables.

**Merging a PR that does not touch `terraform/`.** HCP queues no VCS run for it,
so the `Terraform Cloud/...` check stays pending forever and the PR never
satisfies the branch protection in the UI. Once `all-checks-passed` is green,
merge through the GitHub API with admin rather than waiting on that check.

Pull request CI is the single `.github/workflows/ci.yml`: one changed-paths job
fans out to the backend, frontend, runner and terraform slices and the terminal
`all-checks-passed` job is the required check.

## End-to-end

`backend/e2e/` extends the shared `webbpulse.e2e` plugin and is never forked: a
gap belongs upstream in `webbpulse-python`. `e2e.yml` calls the org
`e2e.yml@v3` after Deploy Backend, Deploy Frontend and Deploy Runner, running
the full suite against staging. `e2e-local.yml` calls `e2e-local.yml@v3` on
pull requests against a stack built from source; it is `continue-on-error` and
is deliberately not part of `all-checks-passed`.

The `E2E_*` variables live on the `staging` GitHub Environment. The suite makes
its own login user per run through `/api/auth/e2e/users`, which is gated by
`ephemeral_users_enabled` and so exists outside production only.

`WebbPulse/webbpulse-terraform-staging-e2e` (repository id 1389889459, public) is
the staging end-to-end test repository, part of the staging environment in the
same way as the `webbpulse-terraform-staging-e2e` AWS account. It is declared in
the WebbPulse-Platform repository factory with the topics `webbpulse-terraform`,
`staging` and `e2e`, is load bearing for staging e2e; its `registry-proof` branch and `v0.1.0` tag are the durable registry fixture `backend/e2e/test_registry.py` installs, published before tag webhook publishing and connected to no module. Its `registry-backfill-fixture` branch (a root `main.tf` only) carries the `v0.2.0` tag and the non-semver `registry-backfill-fixture` tag that `backend/e2e/test_registry_backfill.py` imports on connect and on resync; keep both tags where they are.
The staging GitHub App is installed on it and not on this repository, it carries
the caller workflow and a copy of `examples/first-run`, and the staging
`first-run` workspace is bound to it. VCS runs, check runs and the pull request
comment are proven there, and `E2E_VCS_CONNECT_REPO` names it.
