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
    run_role_arn: 'arn:aws:iam::123456789012:role/terraform-run',
    run_role_setup: {
      principal_arn: 'arn:aws:iam::210987654321:role/control-plane-runner',
      principal_arns: ['arn:aws:iam::210987654321:role/control-plane-runner'],
      external_id: 'ws-01J000000000000000000000',
      role_name: 'control-plane-workspace-ws-01J000000000000000000000',
    },
    run_role_checked_at: null,
    run_role_account_id: null,
    created_at: '2026-09-17T00:00:00Z',
    updated_at: null,
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
    description: '',
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
    key: 'configs/ws-01J000000000000000000000/cv-01J000000000000000000000.tar.gz',
    status: 'uploaded',
    size_bytes: 2048,
    created_at: '2026-09-17T00:00:00Z',
    updated_at: '2026-09-17T00:00:05Z',
    ...overrides,
  };
}

/** A run in the given status, overridable field by field. */
export function aRun(
  status: RunState = 'planned',
  overrides: Partial<Run> = {}
): Run {
  return {
    run_id: 'run-01J000000000000000000000',
    workspace_id: 'ws-01J000000000000000000000',
    config_version_id: 'cv-01J000000000000000000000',
    status,
    plan_only: false,
    message: 'Seed the stack.',
    changes: { add: 3, change: 1, destroy: 0 },
    error: null,
    created_at: '2026-09-17T00:00:00Z',
    updated_at: null,
    started_at: '2026-09-17T00:00:01Z',
    finished_at: null,
    queued_behind: null,
    execution_arn: null,
    ...overrides,
  };
}
