import { ApiError } from '@webbpulse/api-client';
import { describe, expect, it } from 'vitest';

import { aFreshWorkspace, aWorkspace } from '../test-helpers/fixtures';
import {
  ADMINISTRATOR_POLICY_ARN,
  SNIPPET_FORMATS,
  isConnected,
  isRunRoleMissing,
  roleNameFromArn,
  runRoleArnProblem,
  runRolePrefix,
  snippetFor,
  trustPolicy,
} from './runRoleSetup';

const setup = aWorkspace().run_role_setup;

describe('trustPolicy', () => {
  it('names every runner principal and binds the external id', () => {
    const statement = trustPolicy(setup).Statement[0];
    expect(statement?.Principal).toEqual({ AWS: setup.principal_arns });
    expect(statement?.Action).toBe('sts:AssumeRole');
    expect(statement?.Condition).toEqual({
      StringEquals: { 'sts:ExternalId': setup.external_id },
    });
  });

  it('falls back to the single principal when the list is empty', () => {
    const statement = trustPolicy({ ...setup, principal_arns: [] })
      .Statement[0];
    expect(statement?.Principal).toEqual({ AWS: [setup.principal_arn] });
  });
});

describe('snippetFor', () => {
  it.each(SNIPPET_FORMATS.map((format) => format.id))(
    'fills the %s snippet with the external id, both principals and the role name',
    (format) => {
      const snippet = snippetFor(format, setup);
      expect(snippet).toContain(setup.external_id);
      for (const arn of setup.principal_arns) {
        expect(snippet).toContain(arn);
      }
      if (format !== 'trust') {
        expect(snippet).toContain(setup.role_name);
        expect(snippet).toContain(ADMINISTRATOR_POLICY_ARN);
      }
    }
  );

  it('embeds a parseable trust policy in the CLI command', () => {
    const cli = snippetFor('cli', setup);
    const document = /--assume-role-policy-document '(.+)'/.exec(cli)?.[1];
    expect(document).toBeDefined();
    expect(JSON.parse(document ?? '')).toEqual(trustPolicy(setup));
  });
});

describe('runRolePrefix', () => {
  it('keeps the name up to and including -workspace-', () => {
    expect(runRolePrefix('webbpulse-terraform-staging-workspace-01J')).toBe(
      'webbpulse-terraform-staging-workspace-'
    );
    expect(runRolePrefix('no-marker')).toBe('no-marker');
  });
});

describe('roleNameFromArn', () => {
  it('reads the name off a role ARN, with or without a path', () => {
    expect(roleNameFromArn('arn:aws:iam::123456789012:role/a-role')).toBe(
      'a-role'
    );
    expect(
      roleNameFromArn('arn:aws:iam::123456789012:role/platform/a-role')
    ).toBe('a-role');
    expect(roleNameFromArn('arn:aws:iam::123456789012:user/tyler')).toBeNull();
    expect(roleNameFromArn('a-role')).toBeNull();
  });
});

describe('runRoleArnProblem', () => {
  it('accepts a role ARN carrying the assumable prefix', () => {
    expect(
      runRoleArnProblem(aWorkspace().run_role_arn ?? '', setup)
    ).toBeNull();
  });

  it('explains a malformed ARN with the suggested role name', () => {
    expect(runRoleArnProblem('not-an-arn', setup)).toBe(
      `Enter a role ARN like arn:aws:iam::123456789012:role/${setup.role_name}.`
    );
  });

  it('explains a role name outside the assumable prefix', () => {
    expect(
      runRoleArnProblem('arn:aws:iam::123456789012:role/terraform-run', setup)
    ).toBe(
      `The role name must start with ${runRolePrefix(setup.role_name)} for the runner to assume it.`
    );
  });
});

describe('isRunRoleMissing', () => {
  it('recognises the API envelope carrying RUN_ROLE_MISSING', () => {
    const error = new ApiError({
      status: 409,
      statusText: 'Conflict',
      url: 'https://api.test/runs',
      method: 'POST',
      body: {
        success: false,
        status: 409,
        message: 'The workspace has no run role.',
        request_id: 'r-1',
        error_code: 'RUN_ROLE_MISSING',
      },
    });
    expect(isRunRoleMissing(error)).toBe(true);
    expect(isRunRoleMissing(new Error('RUN_ROLE_MISSING'))).toBe(false);
  });
});

describe('isConnected', () => {
  it('needs both a saved ARN and an account from the last check', () => {
    expect(isConnected(aWorkspace())).toBe(true);
    expect(isConnected(aFreshWorkspace())).toBe(false);
    expect(
      isConnected(aFreshWorkspace({ run_role_arn: aWorkspace().run_role_arn }))
    ).toBe(false);
  });
});
