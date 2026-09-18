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
    db/            table names, repositories, condition helpers
  domains/
    workspaces/    workspaces, variables, config versions
    runs/          runs, the runner's bundle and phase results
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

## Auth

A person arrives with a JWT the API Gateway authorizer has already verified. An
agent arrives with a `wpk_` API key. Both render as the same claims object, so a
route guarded by `require_scopes` cannot tell them apart. The scopes are
`workspaces:{read,write}`, `variables:{read,write}`, `configs:{read,write}`,
`runs:{read,write,apply}`.

The runner is separate. Starting a run mints a `wpk_` key scoped `runner`, bound
to that run and expiring after four hours, and only that token opens
`GET /runs/{id}/bundle` and `POST /runs/{id}/phase-result`. The bundle carries
decrypted sensitive variables, so no human scope reaches it, and every terminal
transition revokes the token.

## Sensitive variables

A variable marked sensitive is sealed app side with AES-256-GCM under a key
derived by HKDF from the app secret's `variables_master_key`, following the same
envelope pattern as TOTP. The encryption context binds the ciphertext to one
workspace and key, so a row copied to another key will not decrypt. The API never
returns a sensitive value; the run bundle is the only reader.

## Runs

Runs are serial per workspace: a run created while another is active is stored
`pending` with `queued_behind` set, and the previous run's terminal transition
promotes the next one. Two concurrent executions would otherwise contend on the
S3 state lock.

A run's phase comes from its stored status, never from the caller, because the
plan phase gets a read-only IAM session policy and the apply phase an
unrestricted one.
