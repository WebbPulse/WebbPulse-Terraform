/**
 * Contract types for the WebbPulse Terraform control plane API.
 *
 * Every type here is an alias into `schema.d.ts`, which `npm run api:generate`
 * derives from the backend's own OpenAPI document. Nothing in this file is
 * hand-written, so a backend schema change lands here as a type error rather
 * than as a runtime surprise. See frontend/README.md to regenerate.
 */

import type { components, paths } from './schema';

type Schemas = components['schemas'];

/**
 * The engine a workspace plans and applies with.
 *
 * `NonNullable` because the property is optional on the wire, carrying a
 * default the backend fills in, while this names the value itself. A form's
 * engine state is always one of the two.
 */
export type Engine = NonNullable<Schemas['Workspace']['engine']>;

/** A workspace as the API returns it. */
export type Workspace = Schemas['Workspace'];

/** The body that creates a workspace. */
export type WorkspaceCreate = Schemas['WorkspaceCreate'];

/**
 * What a workspace hands a person so they can create its run role.
 *
 * `principal_arns` names every runner task role, one per phase. A trust policy
 * naming only the plan role passes a plan only run and then fails at apply, so
 * the snippets name them all.
 */
export type RunRoleSetup = Schemas['RunRoleSetup'];

/**
 * Whether the runner can assume a workspace's run role, from its own record.
 *
 * The API never calls STS. `status` is `connected` or `failed` from the newest
 * run on the current ARN that reached the runner's AssumeRole, and
 * `unverified` while no run has, so a plan only run is the check.
 */
export type RunRoleCheck = Schemas['RunRoleCheck'];

/**
 * The body that edits a workspace. Every field is optional.
 *
 * The name is absent because the backend's update model omits it: a rename
 * would break both the state key, which is derived from the workspace id, and
 * the uniqueness claim on the name.
 */
export type WorkspaceUpdate = Schemas['WorkspaceUpdate'];

/** Every workspace, newest last by id. */
export type WorkspaceList = Schemas['WorkspaceList'];

/** Whether a variable is passed to Terraform or to the process environment. */
export type VariableCategory = Schemas['Variable']['category'];

/**
 * A workspace variable as the API returns it.
 *
 * A sensitive variable never carries `value`: the backend seals it app-side and
 * the read routes omit it, so the form treats it as write-only.
 */
export type Variable = Schemas['Variable'];

/** The body that creates or replaces a variable. */
export type VariableWrite = Schemas['VariableWrite'];

/** Every variable on a workspace, by key. */
export type VariableList = Schemas['VariableList'];

/** Where a configuration version is in its upload lifecycle. */
export type ConfigVersionStatus = Schemas['ConfigVersion']['status'];

/** A configuration version as the API returns it. */
export type ConfigVersion = Schemas['ConfigVersion'];

/** The body that creates a configuration version. */
export type ConfigVersionCreate = Schemas['ConfigVersionCreate'];

/**
 * A new configuration version plus the presigned PUT to upload its tarball to.
 *
 * The upload goes straight to S3, so the tarball never passes through Lambda.
 * Every header in `headers` is inside the signature, so a PUT that omits or
 * changes one is rejected by S3.
 */
export type ConfigVersionUpload = Schemas['ConfigVersionUpload'];

/** One workspace's configuration versions, newest last. */
export type ConfigVersionList = Schemas['ConfigVersionList'];

/** The state of a run. */
export type RunState = Schemas['Run']['status'];

/**
 * Every state a run can be in, in the contract's order.
 *
 * A runtime array because the tests iterate it. The annotation ties it to the
 * generated union, so a state added or removed backend-side fails to compile
 * here until this list is brought back into line.
 */
export const RUN_STATES: readonly RunState[] = [
  'pending',
  'planning',
  'planned',
  'awaiting_confirmation',
  'applying',
  'applied',
  'planned_and_finished',
  'errored',
  'cancelled',
  'discarded',
];

/** The resource counts a plan reported. */
export type RunChanges = Schemas['RunChanges'];

/** A run as the API returns it. */
export type Run = Schemas['Run'];

/**
 * A newly created run.
 *
 * `run_token` is present only when the run started: a queued run has no
 * execution and so no token until the run ahead of it finishes.
 */
export type RunCreated = Schemas['RunCreated'];

/** The body that starts a run. */
export type RunCreate = Schemas['RunCreate'];

/** One workspace's runs, newest first. */
export type RunList = Schemas['RunList'];

/** Which phase's log stream to read. */
export type RunPhase = Schemas['LogPage']['phase'];

/** One log line as the logs route returns it. */
export type RunLogLine = Schemas['LogEvent'];

/**
 * A page of log lines.
 *
 * `next_after` is the cursor to pass back as `after`, so the viewer tails the
 * stream rather than re-reading it. It is null when the stream does not exist
 * yet, which is not the same as an empty page.
 */
export type RunLogPage = Schemas['LogPage'];

/**
 * A run's plan, summarised and redacted for the viewer.
 *
 * The raw `terraform show -json` document never crosses the wire: the backend
 * projects it into this shape and replaces every value the plan marked
 * sensitive, so no sealed variable reaches the browser.
 */
export type RunPlan = Schemas['RunPlan'];

/** One resource's entry in a plan. */
export type PlanResourceChange = Schemas['PlanResourceChange'];

/** One root output's change in a plan. */
export type PlanOutputChange = Schemas['PlanOutputChange'];
/** One root output's value after a successful apply, redacted when sensitive. */
export type AppliedOutput = Schemas['AppliedOutput'];

/**
 * What one resource or output is doing in the plan.
 *
 * Terraform writes a replacement as a two element action list whose order is a
 * provider detail. The backend collapses both orders to `replace`.
 */
export type PlanAction = Schemas['PlanResourceChange']['action'];

/** Whether a change is to a managed resource or to a data source read. */
export type PlanMode = Schemas['PlanResourceChange']['mode'];

/** The error envelope every failing route renders. */
export type ErrorResponse = Schemas['ErrorResponse'];

/** The query the runs list accepts, so a caller cannot invent a filter. */
export type RunListQuery = NonNullable<
  paths['/api/v1/runs']['get']['parameters']['query']
>;

/** The query a logs page takes: which phase, and where to resume from. */
export type RunLogsQuery = NonNullable<
  paths['/api/v1/runs/{run_id}/logs']['get']['parameters']['query']
>;

/** Whether this environment has a GitHub App, and what the settings page may offer. */
export type GitHubAppStatus = Schemas['GitHubAppStatus'];

/** Where a new App is created: a personal account, or a named organization. */
export type ManifestStartRequest = Schemas['ManifestStartRequest'];

/** The form action and manifest the SPA posts to GitHub. */
export type ManifestStart = Schemas['ManifestStart'];

/** The create callback's code and state, forwarded to the API. */
export type ManifestConversionRequest = Schemas['ManifestConversionRequest'];

/** The App's install URL, carrying a one-time state. */
export type InstallStart = Schemas['InstallStart'];

/** The setup callback's query, forwarded to the API. */
export type InstallationCallback = Schemas['InstallationCallback'];

/** One installation of the App, as GitHub last confirmed it. */
export type Installation = Schemas['Installation'];

/** Every stored installation. */
export type InstallationList = Schemas['InstallationList'];

/** A repository an installation can reach. */
export type Repository = Schemas['Repository'];

/** Every repository an installation can reach. */
export type RepositoryList = Schemas['RepositoryList'];
