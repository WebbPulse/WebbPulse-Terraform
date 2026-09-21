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
build argument (`workspaces` or `runs`). The base image lives in the Artifacts
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
`DOMAINS` map carries `workspaces` and `runs`. Adding one is a package under
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
runner routes and the anonymous identity documents do not. The scopes are
`workspaces:{read,write}`, `variables:{read,write}`, `configs:{read,write}` and
`runs:{read,write,apply}`.

The runner is separate. Starting a run mints a `wpk_` key scoped `runner`, bound
to that run and expiring after four hours, and only that token opens
`GET /runs/{id}/bundle` and `POST /runs/{id}/phase-result`. The bundle carries
decrypted sensitive variables, so no human scope reaches it, and every terminal
transition revokes the token.

### The run role

A workspace's run role is optional at create, since its trust policy names the
workspace id as the external id and so the id has to exist first. Every workspace
response carries `run_role_setup` (the runner task roles to trust, the external id
and the derived role name), and `POST /workspaces/{id}/run-role/check` assumes the
role and reports `{connected, account_id, error}`, stamping
`run_role_checked_at` and `run_role_account_id`. A run created against a workspace
with no run role is a 409 carrying `RUN_ROLE_MISSING`.

### Runs

Runs are serial per workspace. A run created while another is active is stored
`pending` with `queued_behind` set, and the previous run's terminal transition
promotes it, because two concurrent executions would contend on the S3 state
lock. A run's phase comes from its stored status, never from the caller: the plan
phase gets a read-only IAM session policy and the apply phase an unrestricted
one.

A Step Functions DynamoDB integration cannot carry a task token, so a
confirmation is sent to the `run-confirmations` SQS queue instead and consumed by
the runs function through an event source mapping. That route mounts at the root
rather than under `/api/v1`, and the HTTP API never lists it, so the only way to
reach it is the Lambda Web Adapter's pass-through path.

### Sensitive variables

A variable marked sensitive is sealed app side with AES-256-GCM under a key
derived by HKDF from the app secret's `variables_master_key`. The encryption
context binds the ciphertext to one workspace and key, so a row copied to another
key will not decrypt. The API never returns a sensitive value; the run bundle is
the only reader.

### Runner protocol

Step Functions starts the runner with `runTask.waitForTaskToken`. It fetches the
bundle, unpacks the config tarball, writes the S3 backend override and the auto
loaded tfvars, assumes the workspace's run role with the phase session policy,
runs the engine (`terraform` or `tofu`, from the bundle), streams redacted output
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
