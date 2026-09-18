/** Contract shaped fixtures the tests build their responses from. */

import type { ConfigVersion, Run, RunState, Variable, Workspace } from '../api';

/** A workspace, overridable field by field. */
export function aWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    workspace_id: 'ws-01J000000000000000000000',
    name: 'platform',
    description: 'The platform workspace.',
    engine: 'terraform',
    engine_version: '1.11.0',
    working_directory: 'terraform',
    auto_apply: false,
    run_role_arn: null,
    created_at: '2026-09-17T00:00:00Z',
    updated_at: null,
    latest_run_id: null,
    ...overrides,
  };
}

/** A variable, overridable field by field. */
export function aVariable(overrides: Partial<Variable> = {}): Variable {
  return {
    workspace_id: 'ws-01J000000000000000000000',
    key: 'region',
    value: 'us-west-2',
    category: 'terraform',
    sensitive: false,
    hcl: false,
    description: null,
    created_at: '2026-09-17T00:00:00Z',
    updated_at: null,
    ...overrides,
  };
}

/** A configuration version, overridable field by field. */
export function aConfigVersion(
  overrides: Partial<ConfigVersion> = {}
): ConfigVersion {
  return {
    config_version_id: 'cv-01J000000000000000000000',
    workspace_id: 'ws-01J000000000000000000000',
    status: 'uploaded',
    size_bytes: 2048,
    message: 'Seed the stack.',
    created_at: '2026-09-17T00:00:00Z',
    uploaded_at: '2026-09-17T00:00:05Z',
    ...overrides,
  };
}

/** A run in the given state, overridable field by field. */
export function aRun(
  state: RunState = 'planned',
  overrides: Partial<Run> = {}
): Run {
  return {
    run_id: 'run-01J000000000000000000000',
    workspace_id: 'ws-01J000000000000000000000',
    config_version_id: 'cv-01J000000000000000000000',
    state,
    plan_only: false,
    message: 'Seed the stack.',
    changes: { add: 3, change: 1, destroy: 0 },
    has_changes: true,
    error_message: null,
    created_at: '2026-09-17T00:00:00Z',
    updated_at: null,
    plan_started_at: '2026-09-17T00:00:01Z',
    plan_finished_at: '2026-09-17T00:00:40Z',
    apply_started_at: null,
    apply_finished_at: null,
    ...overrides,
  };
}
