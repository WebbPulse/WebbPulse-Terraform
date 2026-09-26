/** The read-only run role check, shared by every surface that shows the account. */

import { useEffect } from 'react';
import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import { api, type RunRoleCheck, type Workspace } from '../api';
import { workspaceKeys } from './workspaceContext';

/** How often the check is read while nothing prompts it sooner. */
export const RUN_ROLE_CHECK_INTERVAL_MS = 60_000;

/** One answer and the role ARN it was asked about. */
interface RunRoleReading {
  arn: string;
  check: RunRoleCheck | null;
}

/**
 * Polls `GET run-role/check` for a workspace that has a role ARN saved.
 *
 * The read never writes, so it can run wherever the account is shown. It is
 * registered under the workspace's `runRoleCheck` key, so a manual check or a
 * run finishing refreshes every mounted reader at once. An answer is returned
 * only while it belongs to the ARN still saved, and a new ARN is read at once.
 */
export function useRunRoleCheck(
  workspace: Workspace | null,
  intervalMs: number = RUN_ROLE_CHECK_INTERVAL_MS
): RunRoleCheck | null {
  const auth = useQueryAuth();
  const workspaceId = workspace?.workspace_id ?? '';
  const arn = workspace?.run_role_arn ?? null;
  const query = usePolledQuery<RunRoleReading>(
    async ({ signal }) => {
      const asked = arn ?? '';
      const check = await api.readRunRoleCheck(workspaceId, { signal });
      return { arn: asked, check: check ?? null };
    },
    {
      intervalMs,
      queryKey: workspaceKeys(workspaceId).runRoleCheck,
      auth,
      enabled: workspaceId !== '' && arn !== null,
    }
  );
  const { data, refetch } = query;

  useEffect(() => {
    if (arn !== null) {
      void refetch();
    }
  }, [arn, refetch]);

  return data !== null && data.arn === arn ? data.check : null;
}
