/** The run page's polled read of one run, kept alive until the run settles. */

import { useEffect, useState } from 'react';
import {
  usePolledQuery,
  type PolledQueryResult,
} from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import { api, isTerminal, type Run } from '../api';

/** How often a run that has not settled is read. */
export const RUN_POLL_INTERVAL_MS = 3000;

/** The longest the poll backs off after failures, so a blip never parks it for minutes. */
export const RUN_POLL_MAX_BACKOFF_MS = 15_000;

/** How long one read may take before it is abandoned and the next is scheduled. */
export const RUN_READ_DEADLINE_MS = 20_000;

/** How long the poll may go without an attempt starting or settling before it is restarted. */
export const RUN_POLL_STALL_MS = RUN_POLL_MAX_BACKOFF_MS + RUN_READ_DEADLINE_MS;

/**
 * Polls one run every few seconds until it reaches a terminal state.
 *
 * The polled query bounds each read with a deadline, caps the backoff after
 * failures and restarts a stalled loop while the page is visible. Once the run
 * is terminal the poll stops, because a terminal run never changes again.
 */
export function useRunQuery(runId: string): PolledQueryResult<Run> {
  const auth = useQueryAuth();
  const [settledRunId, setSettledRunId] = useState<string | null>(null);
  const query = usePolledQuery<Run>(
    ({ signal }) => api.getRun(runId, { signal }),
    {
      intervalMs: RUN_POLL_INTERVAL_MS,
      maxBackoffMs: RUN_POLL_MAX_BACKOFF_MS,
      attemptTimeoutMs: RUN_READ_DEADLINE_MS,
      stallTimeoutMs: RUN_POLL_STALL_MS,
      queryKey: `run:${runId}`,
      auth,
      enabled: runId !== '' && settledRunId !== runId,
    }
  );
  const { data } = query;
  const terminal =
    data !== null && data.run_id === runId && isTerminal(data.status);

  useEffect(() => {
    if (terminal) {
      setSettledRunId(runId);
    }
  }, [terminal, runId]);

  return query;
}
