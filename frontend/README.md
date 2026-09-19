# Frontend

React 19 and TypeScript on Vite, styled with Tailwind CSS. The operator surface
for the control plane: workspaces, variables, configuration versions and runs.

## Routes

| Path                       | What it is                                 |
| -------------------------- | ------------------------------------------ |
| `/sign-in`                 | The only unauthenticated route             |
| `/workspaces`              | The workspaces list and the create form    |
| `/workspaces/:workspaceId` | Overview, variables, config versions, runs |
| `/runs`                    | Every run in the environment               |
| `/runs/:runId`             | State, plan counts, the log tail, actions  |

## Getting started

Prerequisites: Node 22, npm, and an AWS login to the WebbPulse Identity Center
for the shared packages.

```bash
npm install       # fetch a CodeArtifact token first, below
npm run dev:local # :5173, proxies /api to localhost:8000
```

### Shared packages from CodeArtifact

`@webbpulse/api-client`, `auth`, `config`, `discovery`, `tsconfig` and
`eslint-config` are published to CodeArtifact rather than the public registry,
all resolved at `>=0.13.0 <1` so a build picks up the newest release.
`frontend/.npmrc` points the `@webbpulse` scope at that repository but
deliberately holds no token. Before your first install, fetch a 12 hour one:

```bash
AWS_PROFILE=WebbPulse-Artifacts/AdministratorAccess AWS_REGION=us-west-2 \
  aws codeartifact login --tool npm \
    --domain webbpulse --domain-owner 432410731887 \
    --repository npm --namespace @webbpulse
```

That appends the token to `~/.npmrc`, leaving the checked-in `frontend/.npmrc`
untouched. Re-run it when an install starts returning 401. A `ReadOnlyAccess`
profile is not enough: the AWS managed policy omits `sts:GetServiceBearerToken`,
which `codeartifact login` requires. CI obtains its token over OIDC.

## Scripts

| Command                          | What it does                                                     |
| -------------------------------- | ---------------------------------------------------------------- |
| `npm run dev:local`              | Dev server on :5173, proxying `/api` to localhost:8000           |
| `npm run dev:remote-api`         | Dev server against `https://api.staging.terraform.webbpulse.com` |
| `npm run build`                  | `tsc -b` then a Vite production build                            |
| `npm run api:generate`           | Regenerate `src/api/openapi.json` and `src/api/schema.d.ts`      |
| `npm run lint`, `lint:fix`       | ESLint                                                           |
| `npm run format`, `format:check` | Prettier                                                         |
| `npm run test`                   | Vitest in watch mode                                             |
| `npm run test:run`               | Vitest once. CI appends `-- --coverage`                          |
| `npm run preview`                | Serve the built bundle                                           |

There is no `npm run dev`. Use `dev:local`.

## Configuration

The bundle's configuration comes from the deploy workflow's build step only.
There is no `.env` file.

| Variable            | Meaning                                                                                                                               |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `VITE_API_BASE_URL` | API base. Defaults to `https://api.terraform.webbpulse.com/api/v1` in a production build and `http://localhost:8000/api/v1` otherwise |

In local dev Vite proxies `/api/*` to `http://localhost:8000`, so neither needs
setting.

## Auth

Auth is `AuthClient` from `@webbpulse/auth`: an in-memory access token, an
httpOnly refresh cookie and one retry on a 401. `App.tsx` hands `AuthProvider`
the client `TerraformApi` itself refreshes through, so the guards and the API
share one session rather than two tokens.

`RequireAuth` holds a route back on `isLoading`, which is true only until the
session settles once, and redirects on `!isAuthenticated`. `RequireGuest` waits
on `isBusy` as well, so a sign-in form mid-request is not unmounted and an MFA
challenge is not lost with it.

## API layer

`src/api/` is the whole surface:

| File              | What it holds                                                   |
| ----------------- | --------------------------------------------------------------- |
| `openapi.json`    | The backend's OpenAPI document, generated. Do not edit          |
| `schema.d.ts`     | Types generated from that document. Do not edit                 |
| `types.ts`        | The contract types, each an alias into `schema.d.ts`            |
| `runStates.ts`    | Which actions each run state allows, and how it is badged       |
| `runRoleSetup.ts` | The run role contract and the trust policy and role snippets    |
| `client.ts`       | `TerraformApi`, one method per route, plus the presigned upload |

`TerraformApi` wraps `@webbpulse/api-client`, which rejects on a non-2xx, so the
pages read failures off `usePolledQuery` and `useMutationWithRefetch` rather
than an envelope. `uploadConfigTarball` is deliberately a bare `fetch`: the URL
is presigned for S3, so the request must carry neither the API's auth header nor
its cookies.

Sensitive variable values are write-only. The API never returns them, so editing
one starts with an empty box and a save re-enters the value.

### Workspace setup

A workspace is created from its name alone. The run role comes afterwards, on
the workspace page, which leads with a three step checklist until the first
plan has run: connect an AWS account, upload a configuration, run a plan. Each
workspace response carries `run_role_setup`, the role name, the runner
principals and the external id, and the connect step renders the trust policy
and a Terraform, CloudFormation and AWS CLI snippet from those values. Saving
the role ARN is a `PATCH` of `run_role_arn`; `POST
/workspaces/{id}/run-role/check` assumes the role once and persists the outcome
as `run_role_checked_at` and `run_role_account_id`. Run starts are disabled
until a check has passed, and a `409` carrying `RUN_ROLE_MISSING` renders the
same sentence.

`runRoleSetup.ts` types this contract by hand and overrides the generated
`Workspace` shapes through `index.ts` until the backend change lands in
`openapi.json`. Once it does, the overrides collapse into aliases.

### Regenerating the types

`openapi.json` and `schema.d.ts` are generated, never hand-edited. `types.ts`
holds only aliases into them, so a backend schema change becomes a type error
here rather than a runtime surprise.

```bash
npm run api:generate
```

That builds every domain through the backend's own composition, writes the
merged document to `src/api/openapi.json` with each path under the gateway
prefix the frontend calls, then runs `openapi-typescript` over it to produce
`src/api/schema.d.ts`. It needs the backend's `uv` environment, so run
`uv sync` in `backend/` first if you have not already. Nothing reaches AWS: the
export uses the same fake table names and in-process identity signer the test
suite does.

Commit both files with the change that moved them. The `api-contract` CI job
regenerates and fails on any diff, so a backend schema change that skips this
step is caught on the pull request rather than in the browser.

`--default-non-nullable=false` is deliberate. Without it every field carrying a
Pydantic default is emitted as required, which would make an omitted
`description` a type error while still letting a genuinely required field like
`run_role_arn` slip through as satisfied.

## Structure

```
src/
├── api/            types, run state gating, the run role setup, the typed client
├── components/     guards, the app shell, the primitives, the badge, the log viewer
├── pages/          sign-in, workspaces, workspace detail, runs, run detail
│   └── workspace/  the setup checklist, the connect panel, the four tabs
├── styles/         the Tailwind theme: the surface and brand scales, and the
│                   semantic tokens (bg, panel, line, text, accent) built on them
└── test-helpers/   fixtures, the fetch double, the api mock, render helpers
```

## UI

The app is dark only. `styles/globals.css` defines the palette twice: a raw
`surface` and `brand` scale, and semantic tokens (`bg`, `panel`, `raised`,
`line`, `text`, `text-muted`, `text-faint`, `accent`) that every component
uses, so a light theme is a second block of token values rather than a sweep of
class names. `components/` holds the shell (`Layout`, a left rail with the
sections and the session) and the primitives every page is built from:
`PageHeader`, `Tabs`, `Table`, `Field` with `INPUT_CLASS`, `EmptyState`,
`Button`, `Dialog`, `SegmentedControl`, `CodeBlock` and `CopyButton`. Lists
render one of three shapes: a spinner with a sentence while loading, an
`EmptyState` when empty, and the table otherwise, with `ErrorNotice` above any
of them when the read failed.

## Tests

Vitest with Testing Library and jsdom. Mocks are hand-rolled rather than MSW:
`test-helpers/mockFetch.ts` serves a route table keyed by method and path, which
is smaller than a service worker and can assert on the exact request the client
built. `test-helpers/apiMock.ts` is the typed stand-in the page tests swap in
for the module level `api`.
