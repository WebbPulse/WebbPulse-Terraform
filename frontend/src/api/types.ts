/** Contract types for the WebbPulse Terraform control plane API. */

/** The engine a workspace plans and applies with. */
export type Engine = 'terraform' | 'tofu';

/** A workspace as the API returns it. */
export interface Workspace {
  workspace_id: string;
  name: string;
  description?: string | null;
  engine: Engine;
  engine_version: string;
  working_directory?: string | null;
  auto_apply: boolean;
  run_role_arn?: string | null;
  created_at: string;
  updated_at?: string | null;
  latest_run_id?: string | null;
}

/** The body that creates a workspace. */
export interface WorkspaceCreate {
  name: string;
  description?: string;
  engine?: Engine;
  engine_version?: string;
  working_directory?: string;
  auto_apply?: boolean;
  run_role_arn?: string;
}

/** The body that edits a workspace. Every field is optional. */
export type WorkspaceUpdate = Partial<Omit<WorkspaceCreate, 'name'>> & {
  name?: string;
};

/** Whether a variable is passed to Terraform or to the process environment. */
export type VariableCategory = 'terraform' | 'env';

/**
 * A workspace variable as the API returns it.
 *
 * A sensitive variable never carries `value`: the backend encrypts it app-side
 * and the read routes omit it, so the form treats it as write-only.
 */
export interface Variable {
  workspace_id: string;
  key: string;
  value?: string | null;
  category: VariableCategory;
  sensitive: boolean;
  hcl: boolean;
  description?: string | null;
  created_at: string;
  updated_at?: string | null;
}

/** The body that creates or replaces a variable. */
export interface VariableWrite {
  value: string;
  category: VariableCategory;
  sensitive: boolean;
  hcl: boolean;
  description?: string;
}

/** A configuration version as the API returns it. */
export interface ConfigVersion {
  config_version_id: string;
  workspace_id: string;
  status: ConfigVersionStatus;
  size_bytes?: number | null;
  message?: string | null;
  created_at: string;
  uploaded_at?: string | null;
}

/** Where a configuration version is in its upload lifecycle. */
export type ConfigVersionStatus = 'pending' | 'uploaded' | 'errored';

/**
 * A new configuration version plus the presigned PUT to upload its tarball to.
 *
 * The upload goes straight to S3, so the tarball never passes through Lambda.
 */
export interface ConfigVersionUpload {
  config_version: ConfigVersion;
  upload_url: string;
  upload_headers?: Record<string, string> | null;
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
  state: RunState;
  plan_only: boolean;
  message?: string | null;
  changes?: RunChanges | null;
  has_changes?: boolean | null;
  error_message?: string | null;
  created_at: string;
  updated_at?: string | null;
  plan_started_at?: string | null;
  plan_finished_at?: string | null;
  apply_started_at?: string | null;
  apply_finished_at?: string | null;
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
 * stream rather than re-reading it.
 */
export interface RunLogPage {
  run_id: string;
  phase: RunPhase;
  lines: RunLogLine[];
  next_after?: string | null;
  complete: boolean;
}

/** A page of workspaces. */
export interface WorkspaceList {
  workspaces: Workspace[];
  next_cursor?: string | null;
}

/** A page of runs. */
export interface RunList {
  runs: Run[];
  next_cursor?: string | null;
}

/** A page of configuration versions. */
export interface ConfigVersionList {
  config_versions: ConfigVersion[];
  next_cursor?: string | null;
}

/** Every variable on a workspace. */
export interface VariableList {
  variables: Variable[];
}
