/** Contract shaped fixtures the tests build their responses from. */

import type {
  PlanOutputChange,
  PlanResourceChange,
  RunPlan,
} from '../api/runPlan';
import type {
  ConfigVersion,
  Run,
  RunRoleSetup,
  RunState,
  Variable,
  Workspace,
} from '../api';

/**
 * A workspace whose optional run role fields are all present.
 *
 * The generated contract leaves them optional, so without this a value read
 * off a fixture is `string | null | undefined` and cannot be handed back to
 * one under `exactOptionalPropertyTypes`.
 */
type SettledWorkspace = Workspace & {
  run_role_arn: string | null;
  run_role_setup: RunRoleSetup & { principal_arns: string[] };
  run_role_checked_at: string | null;
  run_role_account_id: string | null;
};

/**
 * A workspace, overridable field by field.
 *
 * The run role fields are optional on the wire but always concrete here, so a
 * test can read one back and pass it straight into another fixture.
 */
export function aWorkspace(
  overrides: Partial<SettledWorkspace> = {}
): SettledWorkspace {
  return {
    workspace_id: 'ws-01J000000000000000000000',
    name: 'platform',
    description: 'The platform workspace.',
    engine: 'terraform',
    engine_version: '1.11.0',
    working_directory: 'terraform',
    run_role_arn:
      'arn:aws:iam::123456789012:role/control-plane-workspace-ws-01J000000000000000000000',
    run_role_setup: {
      principal_arn: 'arn:aws:iam::210987654321:role/control-plane-runner',
      principal_arns: ['arn:aws:iam::210987654321:role/control-plane-runner'],
      external_id: 'ws-01J000000000000000000000',
      role_name: 'control-plane-workspace-ws-01J000000000000000000000',
    },
    run_role_checked_at: '2026-09-17T00:05:00Z',
    run_role_account_id: '123456789012',
    created_at: '2026-09-17T00:00:00Z',
    updated_at: null,
    ...overrides,
  };
}

/** A workspace nobody has connected an account to yet. */
export function aFreshWorkspace(
  overrides: Partial<SettledWorkspace> = {}
): SettledWorkspace {
  return aWorkspace({
    run_role_arn: null,
    run_role_checked_at: null,
    run_role_account_id: null,
    ...overrides,
  });
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

/** One resource change, defaulting to a create and overridable field by field. */
export function aResourceChange(
  overrides: Partial<PlanResourceChange> = {}
): PlanResourceChange {
  return {
    address: 'aws_s3_bucket.logs',
    module_address: '',
    mode: 'managed',
    type: 'aws_s3_bucket',
    name: 'logs',
    provider_name: 'registry.terraform.io/hashicorp/aws',
    action: 'create',
    action_reason: '',
    before: null,
    after: { bucket: 'platform-logs', force_destroy: false },
    after_unknown: { arn: true },
    replace_paths: [],
    before_sensitive: {},
    after_sensitive: {},
    ...overrides,
  };
}

/** One output change, defaulting to a create and overridable field by field. */
export function anOutputChange(
  overrides: Partial<PlanOutputChange> = {}
): PlanOutputChange {
  return {
    name: 'bucket_name',
    action: 'create',
    before: null,
    after: 'platform-logs',
    after_unknown: false,
    sensitive: false,
    ...overrides,
  };
}

/**
 * A plan covering every action the run page renders.
 *
 * One resource per action, plus the sensitive and known after apply cases, so
 * a test that walks the list sees each branch of the diff without building its
 * own plan each time.
 */
export function aRunPlan(overrides: Partial<RunPlan> = {}): RunPlan {
  return {
    run_id: 'run-01J000000000000000000000',
    terraform_version: '1.11.0',
    changes: { add: 2, change: 1, destroy: 1 },
    resource_changes: [
      aResourceChange(),
      aResourceChange({
        address: 'aws_iam_role.runner',
        type: 'aws_iam_role',
        name: 'runner',
        action: 'update',
        before: { name: 'runner', max_session_duration: 3600, path: '/' },
        after: { name: 'runner', max_session_duration: 7200, path: '/' },
        after_unknown: {},
      }),
      aResourceChange({
        address: 'aws_sqs_queue.old',
        type: 'aws_sqs_queue',
        name: 'old',
        action: 'delete',
        before: { name: 'old-queue', delay_seconds: 0 },
        after: null,
        after_unknown: {},
      }),
      aResourceChange({
        address: 'aws_db_instance.primary',
        type: 'aws_db_instance',
        name: 'primary',
        action: 'replace',
        action_reason: 'replace_because_cannot_update',
        before: { identifier: 'primary', engine: 'postgres', password: 'old' },
        after: {
          identifier: 'primary-v2',
          engine: 'postgres',
          password: 'new',
        },
        after_unknown: { endpoint: true },
        replace_paths: [['identifier']],
        before_sensitive: { password: true },
        after_sensitive: { password: true },
      }),
      aResourceChange({
        address: 'data.aws_caller_identity.current',
        mode: 'data',
        type: 'aws_caller_identity',
        name: 'current',
        action: 'read',
        before: null,
        after: null,
        after_unknown: { account_id: true },
      }),
      aResourceChange({
        address: 'aws_kms_key.state',
        type: 'aws_kms_key',
        name: 'state',
        action: 'no-op',
        before: { description: 'state', enabled: true },
        after: { description: 'state', enabled: true },
        after_unknown: {},
      }),
    ],
    output_changes: [
      anOutputChange(),
      anOutputChange({
        name: 'database_password',
        action: 'update',
        before: null,
        after: null,
        sensitive: true,
      }),
      anOutputChange({
        name: 'endpoint',
        action: 'create',
        before: null,
        after: null,
        after_unknown: true,
      }),
    ],
    has_changes: true,
    ...overrides,
  };
}

/** A plan that found nothing to do. */
export function anEmptyRunPlan(): RunPlan {
  return aRunPlan({
    changes: { add: 0, change: 0, destroy: 0 },
    resource_changes: [],
    output_changes: [],
    has_changes: false,
  });
}
