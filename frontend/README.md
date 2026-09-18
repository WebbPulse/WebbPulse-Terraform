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

| Command                          | What it does                                                 |
| -------------------------------- | ------------------------------------------------------------ |
| `npm run dev:local`              | Dev server on :5173, proxying `/api` to localhost:8000       |
| `npm run dev:remote-api`         | Dev server against `https://staging.terraform.webbpulse.com` |
| `npm run build`                  | `tsc -b` then a Vite production build                        |
| `npm run lint`, `lint:fix`       | ESLint                                                       |
| `npm run format`, `format:check` | Prettier                                                     |
| `npm run test`                   | Vitest in watch mode                                         |
| `npm run test:run`               | Vitest once. CI appends `-- --coverage`                      |
| `npm run preview`                | Serve the built bundle                                       |

There is no `npm run dev`. Use `dev:local`.

## Configuration

The bundle's configuration comes from the deploy workflow's build step only.
There is no `.env` file.

| Variable            | Meaning                                                                                                                           |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `VITE_API_BASE_URL` | API base. Defaults to `https://terraform.webbpulse.com/api/v1` in a production build and `http://localhost:8000/api/v1` otherwise |

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

| File           | What it holds                                                   |
| -------------- | --------------------------------------------------------------- |
| `types.ts`     | The contract types: workspace, variable, config version, run    |
| `runStates.ts` | Which actions each run state allows, and how it is badged       |
| `client.ts`    | `TerraformApi`, one method per route, plus the presigned upload |

`TerraformApi` wraps `@webbpulse/api-client`, which rejects on a non-2xx, so the
pages read failures off `usePolledQuery` and `useMutationWithRefetch` rather
than an envelope. `uploadConfigTarball` is deliberately a bare `fetch`: the URL
is presigned for S3, so the request must carry neither the API's auth header nor
its cookies.

Sensitive variable values are write-only. The API never returns them, so editing
one starts with an empty box and a save re-enters the value.

## Structure

```
src/
├── api/            types, run state gating, the typed client
├── components/     guards, layout, the badge, the log viewer
├── pages/          sign-in, workspaces, workspace detail, runs, run detail
│   └── workspace/  the four workspace detail tabs
├── styles/         global styles
└── test-helpers/   fixtures, the fetch double, the api mock, render helpers
```

## Tests

Vitest with Testing Library and jsdom. Mocks are hand-rolled rather than MSW:
`test-helpers/mockFetch.ts` serves a route table keyed by method and path, which
is smaller than a service worker and can assert on the exact request the client
built. `test-helpers/apiMock.ts` is the typed stand-in the page tests swap in
for the module level `api`.
