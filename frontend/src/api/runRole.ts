/** Validation for the per workspace run role ARN. */

const RUN_ROLE_ARN_PATTERN = /^arn:aws:iam::\d{12}:role\/.+$/;

/** The message shown when a run role ARN does not look like one. */
export const RUN_ROLE_ARN_MESSAGE =
  'Enter a role ARN like arn:aws:iam::123456789012:role/terraform-run.';

/**
 * Whether a string looks like an IAM role ARN.
 *
 * A shape check, not an existence check: the API assumes the role at run time
 * and reports what it finds, so the form only catches an obvious typo early.
 */
export function isRunRoleArn(value: string): boolean {
  return RUN_ROLE_ARN_PATTERN.test(value.trim());
}
