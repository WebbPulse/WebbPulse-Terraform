/** Contract types for the WebbPulse Terraform control plane API. */

/** The engine a workspace plans and applies with. */
export type Engine = 'terraform' | 'tofu';

/** A workspace as the API returns it. */
export interface Workspace {
  workspace_id: string;
  name: string;
  description: string;
  engine: Engine;
  engine_version: string;
  working_directory: string;
  run_role_arn: string;
  created_at: string;
  updated_at?: string | null;
}

/** The body that creates a workspace. */
export interface WorkspaceCreate {
  name: string;
  engine?: Engine;
  engine_version: string;
  run_role_arn: string;
  working_directory?: string;
  description?: string;
}

/**
 * The body that edits a workspace. Every field is optional.
 *
 * The name is absent on purpose: a rename would break both the state key, which
 * is derived from the workspace id, and the uniqueness claim on the name, so
 * the backend refuses it by omitting it from its update model.
 */
export interface WorkspaceUpdate {
  engine?: Engine;
  engine_version?: string;
  run_role_arn?: string;
  working_directory?: string;
  description?: string;
}

/** Whether a variable is passed to Terraform or to the process environment. */
export type VariableCategory = 'terraform' | 'env';

/**
 * A workspace variable as the API returns it.
 *
 * A sensitive variable never carries `value`: the backend seals it app-side and
 * the read routes omit it, so the form treats it as write-only.
 */
export interface Variable {
  workspace_id: string;
  key: string;
  value?: string | null;
  category: VariableCategory;
  sensitive: boolean;
  description: string;
  created_at: string;
  updated_at?: string | null;
}

/** The body that creates or replaces a variable. */
export interface VariableWrite {
  value: string;
  category: VariableCategory;
  sensitive: boolean;
  description?: string;
}

/** Where a configuration version is in its upload lifecycle. */
export type ConfigVersionStatus = 'pending' | 'uploaded';

/** A configuration version as the API returns it. */
export interface ConfigVersion {
  config_version_id: string;
  workspace_id: string;
  key: string;
  status: ConfigVersionStatus;
  size_bytes: number;
  created_at: string;
  updated_at?: string | null;
}

/** The body that creates a configuration version. */
export interface ConfigVersionCreate {
  /** The ceiling the presigned PUT signs as `Content-Length`. */
  size_bytes?: number;
}

/**
 * A new configuration version plus the presigned PUT to upload its tarball to.
 *
 * The upload goes straight to S3, so the tarball never passes through Lambda.
 * Every header in `headers` is inside the signature, so a PUT that omits or
 * changes one is rejected by S3.
 */
export interface ConfigVersionUpload {
  config_version: ConfigVersion;
  upload_url: string;
  headers: Record<string, string>;
  expires_in: number;
}

/** Every state a run can be in, in the contract's order. */
export const RUN_STATES = [
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
] as const;

/** The state of a run. */
export type RunState = (typeof RUN_STATES)[number];

/** The resource counts a plan reported. */
export interface RunChanges {
  add: number;
  change: number;
  destroy: number;
}

/** A run as the API returns it. */
export interface Run {
  run_id: string;
  workspace_id: string;
  config_version_id: string;
  status: RunState;
  plan_only: boolean;
  message: string;
  created_at: string;
  updated_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  /** The run this one waits on, when it was queued rather than started. */
  queued_behind?: string | null;
  changes?: RunChanges | null;
  error?: string | null;
  execution_arn?: string | null;
}

/**
 * A newly created run.
 *
 * `run_token` is present only when the run started: a queued run has no
 * execution and so no token until the run ahead of it finishes.
 */
export interface RunCreated extends Run {
  run_token?: string | null;
}

/** The body that starts a run. */
export interface RunCreate {
  workspace_id: string;
  config_version_id: string;
  plan_only: boolean;
  message?: string;
}

/** Which phase's log stream to read. */
export type RunPhase = 'plan' | 'apply';

/** One log line as the logs route returns it. */
export interface RunLogLine {
  timestamp: number;
  message: string;
}

/**
 * A page of log lines.
 *
 * `next_after` is the cursor to pass back as `after`, so the viewer tails the
 * stream rather than re-reading it. It is null when the stream does not exist
 * yet, which is not the same as an empty page.
 */
export interface RunLogPage {
  run_id: string;
  phase: RunPhase;
  events: RunLogLine[];
  next_after?: string | null;
}

/** Every workspace, newest last by id. */
export interface WorkspaceList {
  items: Workspace[];
}

/** One workspace's runs, newest first. */
export interface RunList {
  items: Run[];
}

/** One workspace's configuration versions, newest last. */
export interface ConfigVersionList {
  items: ConfigVersion[];
}

/** Every variable on a workspace, by key. */
export interface VariableList {
  items: Variable[];
}
