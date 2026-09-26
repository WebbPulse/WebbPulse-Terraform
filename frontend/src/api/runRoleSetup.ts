/**
 * The run role setup helpers: trust policy, snippets and connection state.
 *
 * Every shape here is an alias into the generated contract. This module holds
 * only the logic that turns a workspace's `run_role_setup` into something a
 * person can paste, plus the predicates the setup UI reads.
 */

import { ApiError, getWebbPulseError } from '@webbpulse/api-client';

import type { RunRoleCheck, RunRoleSetup, Workspace } from './types';

/** The error code a run start or a check answers when no role is set. */
export const RUN_ROLE_MISSING_CODE = 'RUN_ROLE_MISSING';

/** The one sentence shown wherever a missing role blocks a run. */
export const RUN_ROLE_MISSING_MESSAGE =
  'Connect an AWS account before starting a run.';

/** Whether a thrown error is the API refusing for want of a run role. */
export function isRunRoleMissing(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    getWebbPulseError(error).errorCode === RUN_ROLE_MISSING_CODE
  );
}

/**
 * Whether the workspace has a run role ARN saved, which is all a run needs.
 *
 * The API refuses a run only for a missing ARN. Whether the runner can assume
 * the role is proven by the run itself, so this is what gates run starts.
 */
export function hasRunRole(workspace: Workspace): boolean {
  return (workspace.run_role_arn ?? null) !== null;
}

/**
 * Whether the last recorded check found a run that assumed the role.
 *
 * Both fields are optional on the wire, so an absent one counts as unset
 * exactly as an explicit null does.
 */
export function isConnected(workspace: Workspace): boolean {
  return (
    hasRunRole(workspace) && (workspace.run_role_account_id ?? null) !== null
  );
}

/** The words for a workspace whose account is not shown as connected. */
export function connectionLabel(workspace: Workspace): string {
  return hasRunRole(workspace) ? 'Not verified' : 'Not connected';
}

/** Where a workspace's AWS account stands, as every surface shows it. */
export interface AccountStatus {
  state: 'connected' | 'failed' | 'unverified' | 'missing';
  accountId: string | null;
  checkedAt: string | null;
  error: string | null;
}

/**
 * The one answer to whether the workspace's account is connected.
 *
 * The live check reads the runner's own record of the latest runs, so it wins
 * whenever it has arrived. Until then the fields a manual check stamped on the
 * workspace stand in for it.
 */
export function accountStatus(
  workspace: Workspace,
  check: RunRoleCheck | null
): AccountStatus {
  if (!hasRunRole(workspace)) {
    return { state: 'missing', accountId: null, checkedAt: null, error: null };
  }
  if (check !== null) {
    return {
      state: check.status,
      accountId: check.account_id ?? null,
      checkedAt: check.checked_at ?? null,
      error: check.error ?? null,
    };
  }
  if (isConnected(workspace)) {
    return {
      state: 'connected',
      accountId: workspace.run_role_account_id ?? null,
      checkedAt: workspace.run_role_checked_at ?? null,
      error: null,
    };
  }
  return { state: 'unverified', accountId: null, checkedAt: null, error: null };
}

/** The short words for an account status, or its account id once connected. */
export function accountStatusLabel(status: AccountStatus): string {
  switch (status.state) {
    case 'connected':
      return status.accountId ?? 'Connected';
    case 'failed':
      return 'Connection failed';
    case 'unverified':
      return 'Not verified';
    case 'missing':
      return 'Not connected';
  }
}

/** The marker in a suggested role name that ends the assumable prefix. */
const WORKSPACE_MARKER = '-workspace-';

/**
 * The prefix a role name must start with for the runner to assume it.
 *
 * The runner's task role may assume `arn:aws:iam::*:role/<prefix>*`, where the
 * prefix is the suggested name up to and including `-workspace-`.
 */
export function runRolePrefix(roleName: string): string {
  const at = roleName.indexOf(WORKSPACE_MARKER);
  if (at === -1) {
    return roleName;
  }
  return roleName.slice(0, at + WORKSPACE_MARKER.length);
}

/** The role name an ARN points at, or null when it is not a role ARN. */
export function roleNameFromArn(arn: string): string | null {
  const match = /^arn:aws:iam::\d{12}:role\/(?:.*\/)?([^/]+)$/.exec(arn.trim());
  return match?.[1] ?? null;
}

/**
 * Why a role ARN would not work for this workspace, or null when it would.
 *
 * A shape check and a prefix check, both local: the first run is what proves
 * the role exists and trusts the runner.
 */
export function runRoleArnProblem(
  arn: string,
  setup: RunRoleSetup
): string | null {
  const name = roleNameFromArn(arn);
  if (name === null) {
    return `Enter a role ARN like arn:aws:iam::123456789012:role/${setup.role_name}.`;
  }
  const prefix = runRolePrefix(setup.role_name);
  if (!name.startsWith(prefix)) {
    return `The role name must start with ${prefix} for the runner to assume it.`;
  }
  return null;
}

/** The managed policy the snippets attach, so a first run has what it needs. */
export const ADMINISTRATOR_POLICY_ARN =
  'arn:aws:iam::aws:policy/AdministratorAccess';

/** Every principal the trust policy names, falling back to the first alone. */
export function trustedPrincipals(setup: RunRoleSetup): string[] {
  const all = (setup.principal_arns ?? []).filter((arn) => arn !== '');
  if (all.length > 0) {
    return all;
  }
  return setup.principal_arn === '' ? [] : [setup.principal_arn];
}

/** The one statement a run role's trust policy carries. */
export interface TrustPolicyStatement {
  Sid: string;
  Effect: 'Allow';
  Principal: { AWS: string[] };
  Action: 'sts:AssumeRole';
  Condition: { StringEquals: { 'sts:ExternalId': string } };
}

/** The trust policy document. */
export interface TrustPolicy {
  Version: '2012-10-17';
  Statement: TrustPolicyStatement[];
}

/** The trust policy the run role needs, as a document. */
export function trustPolicy(setup: RunRoleSetup): TrustPolicy {
  return {
    Version: '2012-10-17',
    Statement: [
      {
        Sid: 'WebbPulseTerraformRunner',
        Effect: 'Allow',
        Principal: { AWS: trustedPrincipals(setup) },
        Action: 'sts:AssumeRole',
        Condition: { StringEquals: { 'sts:ExternalId': setup.external_id } },
      },
    ],
  };
}

/** The trust policy as pretty printed JSON. */
export function trustPolicyJson(setup: RunRoleSetup): string {
  return JSON.stringify(trustPolicy(setup), null, 2);
}

/** Indents every line after the first, for nesting a document in a snippet. */
function indent(text: string, spaces: number): string {
  const pad = ' '.repeat(spaces);
  return text
    .split('\n')
    .map((line, index) => (index === 0 ? line : pad + line))
    .join('\n');
}

/** A Terraform snippet creating the role with the trust policy. */
export function terraformSnippet(setup: RunRoleSetup): string {
  return `resource "aws_iam_role" "webbpulse_terraform_run" {
  name               = "${setup.role_name}"
  assume_role_policy = jsonencode(${indent(trustPolicyJson(setup), 2)})
}

resource "aws_iam_role_policy_attachment" "webbpulse_terraform_run" {
  role       = aws_iam_role.webbpulse_terraform_run.name
  policy_arn = "${ADMINISTRATOR_POLICY_ARN}"
}
`;
}

/** A CloudFormation template creating the same role. */
export function cloudFormationSnippet(setup: RunRoleSetup): string {
  return `AWSTemplateFormatVersion: "2010-09-09"
Description: Run role for the WebbPulse Terraform workspace ${setup.external_id}

Resources:
  WebbPulseTerraformRunRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: ${setup.role_name}
      ManagedPolicyArns:
        - ${ADMINISTRATOR_POLICY_ARN}
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Sid: WebbPulseTerraformRunner
            Effect: Allow
            Principal:
              AWS:
${trustedPrincipals(setup)
  .map((arn) => `                - ${arn}`)
  .join('\n')}
            Action: sts:AssumeRole
            Condition:
              StringEquals:
                sts:ExternalId: ${setup.external_id}

Outputs:
  RoleArn:
    Value: !GetAtt WebbPulseTerraformRunRole.Arn
`;
}

/** Two AWS CLI commands creating the role and attaching the policy. */
export function awsCliSnippet(setup: RunRoleSetup): string {
  const document = JSON.stringify(trustPolicy(setup));
  return `aws iam create-role \\
  --role-name ${setup.role_name} \\
  --assume-role-policy-document '${document}'

aws iam attach-role-policy \\
  --role-name ${setup.role_name} \\
  --policy-arn ${ADMINISTRATOR_POLICY_ARN}
`;
}

/** The formats the setup snippets come in. */
export type SnippetFormat = 'terraform' | 'cloudformation' | 'cli' | 'trust';

/** The formats in the order the segmented control shows them. */
export const SNIPPET_FORMATS: readonly {
  id: SnippetFormat;
  label: string;
  language: string;
}[] = [
  { id: 'terraform', label: 'Terraform', language: 'hcl' },
  { id: 'cloudformation', label: 'CloudFormation', language: 'yaml' },
  { id: 'cli', label: 'AWS CLI', language: 'shell' },
  { id: 'trust', label: 'Trust policy', language: 'json' },
];

/** The snippet for a format, with the workspace's values filled in. */
export function snippetFor(format: SnippetFormat, setup: RunRoleSetup): string {
  switch (format) {
    case 'terraform':
      return terraformSnippet(setup);
    case 'cloudformation':
      return cloudFormationSnippet(setup);
    case 'cli':
      return awsCliSnippet(setup);
    case 'trust':
      return trustPolicyJson(setup);
  }
}
