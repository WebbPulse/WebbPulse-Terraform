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
 * naming only the plan role passes the connection check and then fails at
 * apply, so the snippets name them all.
 */
export type RunRoleSetup = Schemas['RunRoleSetup'];

/**
 * The outcome of one AssumeRole against a workspace's run role.
 *
 * `error` is a sentence for a person rather than the STS code, and no part of
 * the temporary credentials reaches it.
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
