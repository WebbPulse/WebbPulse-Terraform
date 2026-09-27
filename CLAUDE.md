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
(`terraform/`), applied by HCP Terraform until cutover.

**Environments:** `staging` serves `staging.terraform.webbpulse.com` with the API
at `api.staging.terraform.webbpulse.com`. `main` serves `terraform.webbpulse.com`
and `api.terraform.webbpulse.com`.

The replacement plan and current status live in `TFC-REPLACEMENT.md` at the
WebbPulse root. Read it there rather than restating it here.

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

Applies happen in HCP Terraform, never locally. The workspaces use dynamic
provider credentials, so a local `terraform plan` has no way to authenticate.

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
agent with a `wpk_` API key that the gate authorizer passes through by prefix and
`claims_or_api_key` verifies in process. Both render as the same claims object,
so a route guarded by `require_scopes` cannot tell them apart. Every product
route in `terraform/apigateway.tf` carries `require_identity_jwt`; only the two
runner routes, `POST /github/webhooks` (a webhook signature), the registry protocol
under `/v1/modules` (a `wpk_` key only) and the anonymous identity documents do not.
The scopes are `workspaces:{read,write}`, `variables:{read,write}`,
`configs:{read,write}`, `runs:{read,write,apply}`, `state:download` and
`registry:{read,write}`.

State history and metadata require `workspaces:read`. Raw state downloads also
require `state:download`, granted to admin sessions and explicitly delegated agent
keys, never ordinary read-only sessions. Existing keys need that scope granted
through a newly minted key. Download URLs expire after 60 seconds, pin one S3
version, and return `Cache-Control: no-store` on both the API and S3 responses.
History cursors are bound to the workspace's state key and invalid ones return 400; clients
must follow `next_page_token` even on an empty page caused by delete markers.

The runner is separate. Starting a run mints a `wpk_` key scoped `runner`, bound
to that run and expiring after four hours, and only that token opens
`GET /runs/{id}/bundle` and `POST /runs/{id}/phase-result`. The bundle carries
decrypted sensitive variables, so no human scope reaches it, and every terminal
transition revokes the token.

### The run role

A workspace's run role is optional at create, since its trust policy names the
workspace id as the external id and so the id has to exist first. Every workspace
response carries `run_role_setup` (the runner task roles to trust, the external id
and the derived role name). `GET /workspaces/{id}/run-role/check` reports
`{connected, status, account_id, error, run_id, checked_at}` from the runner's own
AssumeRole outcome in the newest run on the current ARN, never from an STS call,
so a role no run has tried is `unverified` and a plan only run is the check. The
POST gives the same answer and stamps `run_role_checked_at` and
`run_role_account_id`. The API holds no `sts:AssumeRole` on run roles. A run
created against a workspace with no run role is a 409 carrying `RUN_ROLE_MISSING`.

`POST /workspaces/{id}/run-role/quick-setup` takes an optional account id, saves the
derived ARN when one is given and returns an AWS CloudFormation quick create link. The template
(`app/domains/workspaces/quick_setup.py`) trusts only the runner task roles with
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
plan.

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

Publishing follows HCP's tag based "Publish module from VCS". `POST
/api/v1/registry/modules` (`registry:write`) connects a module to a repository the
GitHub App is installed on, resolved like a workspace's `vcs_repo` (422
`VCS_REPO_NOT_INSTALLED`, 409 `GITHUB_NOT_CONFIGURED` with no App). The namespace is
the owner; the name and provider come from the body or from
`terraform-<provider>-<name>`. The module is the repository root, with no
subdirectory. Connecting imports no existing tags. `GET` and `DELETE
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
nothing. GitHub sends no push event when more than three tags are pushed at once,
so push tags one at a time. In Root A the consumer route is shadowed by the runs
consumer at the same pass-through path, so tests call `route_record` directly.

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

The webhook consumer (below) writes an ingest record to `vcs-uploads` (three day
TTL) and the tarball to `ingest/<upload_id>.tar.gz`. S3 Object Created on `ingest/`
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

Step Functions starts the runner with `runTask.waitForTaskToken`. It fetches the
bundle, unpacks the config tarball, writes the S3 backend override and the auto
loaded tfvars files, assumes the workspace's run role with the phase session policy,
points the S3 backend at the task role through the `webbpulse-state` profile so
the run role only reaches the providers, runs the engine (`terraform` or `tofu`, from the bundle), streams redacted output
to CloudWatch Logs, uploads its artifacts to presigned URLs and reports back
through the task token. Every line passes through `app.logs.Redactor` first, and
the engine's environment is built without the runner's own tokens.

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
- Route guards: a spinner while `isLoading`, a redirect on `!isAuthenticated`,
  and guest guards also wait on `!isBusy`.
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
`runner/` rebuilds the runner image, and `terraform/` is applied by HCP Terraform
from the branch. `all-checks-passed` is the required check.

**Bootstrapping a fresh environment takes two applies.** Lambda resolves an image
tag during `CreateFunction`, so the domain functions cannot exist before their
ECR repositories hold an image. The first apply runs with
`bootstrap_image_tag = ""`, which builds everything but the two functions. The
backend then deploys and pushes `sha-<sha>` for both domains, and the second
apply runs with `bootstrap_image_tag` set by hand as a workspace variable on the
HCP workspace to that tag. After that the deploy workflow owns the image and each
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
`staging` and `e2e`, is load bearing for staging e2e; its `registry-proof` branch and `v0.1.0` tag are the durable registry fixture `backend/e2e/test_registry.py` installs, published before tag webhook publishing and connected to no module.
The staging GitHub App is installed on it and not on this repository, it carries
the caller workflow and a copy of `examples/first-run`, and the staging
`first-run` workspace is bound to it. VCS runs, check runs and the pull request
comment are proven there, and `E2E_VCS_CONNECT_REPO` names it.
