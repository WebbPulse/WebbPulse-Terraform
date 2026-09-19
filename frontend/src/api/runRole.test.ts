import { describe, expect, it } from 'vitest';

import { isRunRoleArn } from './runRole';

describe('isRunRoleArn', () => {
  it('accepts a role ARN, with a path and with surrounding space', () => {
    expect(isRunRoleArn('arn:aws:iam::123456789012:role/terraform-run')).toBe(
      true
    );
    expect(
      isRunRoleArn('arn:aws:iam::123456789012:role/platform/terraform-run')
    ).toBe(true);
    expect(
      isRunRoleArn('  arn:aws:iam::123456789012:role/terraform-run  ')
    ).toBe(true);
  });

  it('rejects an empty value, a wrong account length and a non role ARN', () => {
    expect(isRunRoleArn('')).toBe(false);
    expect(isRunRoleArn('arn:aws:iam::12345:role/terraform-run')).toBe(false);
    expect(isRunRoleArn('arn:aws:iam::123456789012:role/')).toBe(false);
    expect(isRunRoleArn('arn:aws:iam::123456789012:user/tyler')).toBe(false);
    expect(isRunRoleArn('arn:aws:s3:::bucket')).toBe(false);
    expect(isRunRoleArn('terraform-run')).toBe(false);
  });
});
