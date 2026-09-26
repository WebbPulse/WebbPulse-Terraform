import { ApiError } from '@webbpulse/api-client';
import { describe, expect, it } from 'vitest';

import {
  isWorkspaceHasActiveRun,
  isWorkspaceManagesResources,
} from './workspaceDeletion';

/** A 409 from the delete route carrying the given error code. */
function conflict(errorCode: string): ApiError {
  return new ApiError({
    status: 409,
    statusText: 'Conflict',
    url: 'https://api.test/api/v1/workspaces/ws-1',
    method: 'DELETE',
    body: {
      success: false,
      status: 409,
      message: 'Refused.',
      request_id: 'r-1',
      error_code: errorCode,
    },
  });
}

describe('workspace delete refusals', () => {
  it('tells a managed resources refusal from an active run one', () => {
    const managed = conflict('WORKSPACE_MANAGES_RESOURCES');
    const active = conflict('WORKSPACE_HAS_ACTIVE_RUN');
    expect(isWorkspaceManagesResources(managed)).toBe(true);
    expect(isWorkspaceHasActiveRun(managed)).toBe(false);
    expect(isWorkspaceHasActiveRun(active)).toBe(true);
    expect(isWorkspaceManagesResources(active)).toBe(false);
    expect(
      isWorkspaceManagesResources(new Error('WORKSPACE_MANAGES_RESOURCES'))
    ).toBe(false);
  });
});
