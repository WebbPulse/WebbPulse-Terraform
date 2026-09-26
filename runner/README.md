# Runner

The Fargate task that executes one phase of one run. Step Functions starts it
with `runTask.waitForTaskToken`, it fetches everything it needs from the runs
domain, runs Terraform or OpenTofu, and reports back through the task token.

## Layout

| Path | Holds |
| --- | --- |
| `Dockerfile` | linux/arm64 image: pinned Terraform and OpenTofu, git, the uv installed `app` package |
| `versions.env` | the pinned engine versions and their per architecture SHA256 sums |
| `app/models.py` | the runner environment and the bundle the runs domain serves |
| `app/api.py` | bundle fetch, presigned artifact transfers, phase result post |
| `app/workspace.py` | config tarball unpack, the S3 backend override, the two tfvars files |
| `app/credentials.py` | assuming the per workspace run role with the phase session policy |
| `app/engine.py` | the engine subprocess, its environment and plan JSON parsing |
| `app/logs.py` | redaction and the CloudWatch Logs sink |
| `app/callback.py` | `SendTaskSuccess` and `SendTaskFailure` |
| `app/main.py` | the `python -m app.main` entrypoint |
| `pyrightconfig.json` | pyright settings, strict mode |
| `tests/` | pytest with moto for STS and Logs, an httpx mock transport and a fake engine on PATH |

## Commands

```sh
uv sync                              # install, including the dev group
uv run ruff check app tests          # lint
uv run ruff format app tests         # format
uv run pyright                       # type check, strict
uv run pytest                        # tests

docker buildx build --platform linux/arm64 -t webbpulse-terraform-runner:local .
```

`pyrightconfig.json` runs strict, including `tests`. Two rules are off, both
because of how `boto3-stubs` shapes third party types rather than anything in
this code: `reportUnknownMemberType`, because `boto3.client` is one large
overload set whose unstubbed services return `Unknown`, so pyright calls the
whole symbol partially unknown whichever branch is selected, and
`reportTypedDictNotRequiredAccess`, because botocore's response TypedDicts mark
keys `NotRequired` and the tests index them directly. Everything else strict
checks, so an unannotated parameter or a wrong return type still fails.

## Protocol

The task definition supplies `RUNNER_LOG_GROUP` and the state machine's
container overrides supply `RUN_ID`, `WORKSPACE_ID`, `PHASE` (`plan` or
`apply`), `TASK_TOKEN`, `RUN_TOKEN` and `API_BASE_URL`. `RUN_TOKEN` comes from
`$.run_token` on the execution input, which the runs domain sets when it mints
the token. The runner then:

1. `GET {API_BASE_URL}/api/v1/runs/{RUN_ID}/bundle` with the run token as
   bearer, validating the response as `Bundle`: `run_id`, `workspace_id`,
   `engine`, `engine_version`, `config_url`, the nested `backend`, `run_role`
   and `artifacts`, `working_directory` and the three variable maps. The backend
   also sends `phase` and `plan_only`, which the model ignores: the phase comes
   from `PHASE`.
2. Downloads and unpacks the config tarball, refusing members that escape the
   unpack directory, then resolves `working_directory` under it. An absolute
   value, one climbing out with `..`, or one the configuration does not carry
   fails the task before the engine runs. Empty means the tarball root.
3. Writes the S3 backend override with `use_lockfile = true` and the terraform
   variables into the working directory, since neither is loaded from a parent.
   The variables go to two auto loaded files. Literal values go to
   `zz_webbpulse.auto.tfvars.json`, where JSON decoding makes every value what it
   says it is, so a value carrying quotes, braces or `${` cannot be reinterpreted.
   Values the workspace marked HCL go to `zz_webbpulse.auto.tfvars`, the native
   form, written as `key = (\n<value>\n)` so the engine parses each one and the
   value stays inside its own parenthesis, which the backend's write time check
   guarantees it cannot close. That is the only way a `list` or `map` typed input variable can be given a
   value: quoting `["a", "b"]` into the JSON file would hand a `list(string)`
   variable an eight character string instead. A key is in one file or the other,
   never both, so the two auto loaded files never contend.
4. Assumes the bundle's run role with the workspace id as the external id and the
   phase session policy, and exports only those credentials to the engine.
5. Runs the engine from the working directory: `init`, then
   `plan -out plan.tfplan -detailed-exitcode` plus `show -json`
   for the plan phase, or downloads `plan.tfplan` and runs `apply plan.tfplan`
   for the apply phase.
6. Streams the engine's combined output line by line to the `<run_id>/<phase>`
   stream in `RUNNER_LOG_GROUP` and to stdout.
7. Uploads `plan.tfplan` and `plan.json` on a plan phase and the redacted log on
   both. Each one is uploaded by first posting its exact byte count to
   `POST /runs/{RUN_ID}/artifact-uploads` as `{artifact, size_bytes}`, then
   PUTting the bytes to the returned `url` with the returned `headers` sent
   verbatim. The URL signs `Content-Type` and `Content-Length`, so a body of any
   other length is refused and nothing may be added to those headers. It then
   posts `POST /runs/{RUN_ID}/phase-result` and sends task success with
   `{exit_code, changes: {add, change, destroy}}` or task failure.

The engine comes from the bundle's `engine` field, `terraform` or `tofu`.

## Logging

Every line passes through `app.logs.Redactor` before it reaches CloudWatch Logs,
stdout or the uploaded log artifact. The run token, the task token, the assumed
role credentials, the external id and every environment, terraform and HCL variable
value are registered as sensitive, and an HCL value's string and heredoc
literals are registered on their own too, since the engine can print a member
without the rest of the expression. The engine's environment is built without the
runner's own tokens and without the task role's container credentials, so a
provider that dumps its environment cannot leak them.
