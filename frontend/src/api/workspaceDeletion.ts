/**
 * The workspace delete refusals: the error codes the API answers a delete with
 * and the predicates the deletion settings read to pick what to show.
 */

import { ApiError, getWebbPulseError } from '@webbpulse/api-client';

/** The code a safe delete answers while the current state tracks resources. */
export const WORKSPACE_MANAGES_RESOURCES_CODE = 'WORKSPACE_MANAGES_RESOURCES';

/** The code any delete answers while a run on the workspace is unfinished. */
export const WORKSPACE_HAS_ACTIVE_RUN_CODE = 'WORKSPACE_HAS_ACTIVE_RUN';

/** Whether a thrown error is an API refusal carrying the given code. */
function hasErrorCode(error: unknown, code: string): boolean {
  return (
    error instanceof ApiError && getWebbPulseError(error).errorCode === code
  );
}

/** Whether a safe delete was refused because state still tracks resources. */
export function isWorkspaceManagesResources(error: unknown): boolean {
  return hasErrorCode(error, WORKSPACE_MANAGES_RESOURCES_CODE);
}

/** Whether a delete was refused because a run on the workspace is unfinished. */
export function isWorkspaceHasActiveRun(error: unknown): boolean {
  return hasErrorCode(error, WORKSPACE_HAS_ACTIVE_RUN_CODE);
}
