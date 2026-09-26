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

/** How old the last good read may get before the watchdog forces another. */
export const RUN_STALE_AFTER_MS = 20_000;

/** The error a read that outlived its deadline rejects with. */
export class RunReadTimeoutError extends Error {
  constructor() {
    super('Reading the run timed out. Retrying.');
    this.name = 'RunReadTimeoutError';
  }
}

/**
 * Settles `read` within `ms`, aborting it and rejecting when it does not.
 *
 * The polled query holds one attempt in flight and hands every later refetch
 * that same promise, so a read that never settles would stop the poll for good.
 * Racing it against a deadline guarantees the attempt ends and the next is
 * scheduled.
 */
export function withDeadline<T>(
  read: (signal: AbortSignal) => Promise<T>,
  outer: AbortSignal,
  ms: number
): Promise<T> {
  const controller = new AbortController();
  const forward = (): void => {
    controller.abort();
  };
  outer.addEventListener('abort', forward, { once: true });
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new RunReadTimeoutError());
    }, ms);
  });
  return Promise.race([read(controller.signal), deadline]).finally(() => {
    clearTimeout(timer);
    outer.removeEventListener('abort', forward);
  });
}

/** Whether the page is on screen, which is when the watchdog may fire. */
function pageVisible(): boolean {
  return (
    typeof document === 'undefined' || document.visibilityState !== 'hidden'
  );
}

/**
 * Polls one run every few seconds until it reaches a terminal state.
 *
 * Three guards keep the poll moving through every phase change: each read has
 * a deadline, backoff after failures is capped, and a watchdog forces a read
 * whenever the last good one is older than {@link RUN_STALE_AFTER_MS} while the
 * run is unsettled and the page is visible. Once the run is terminal the poll
 * stops, because a terminal run never changes again.
 */
export function useRunQuery(runId: string): PolledQueryResult<Run> {
  const auth = useQueryAuth();
  const [settledRunId, setSettledRunId] = useState<string | null>(null);
  const query = usePolledQuery<Run>(
    ({ signal }) =>
      withDeadline(
        (inner) => api.getRun(runId, { signal: inner }),
        signal,
        RUN_READ_DEADLINE_MS
      ),
    {
      intervalMs: RUN_POLL_INTERVAL_MS,
      maxBackoffMs: RUN_POLL_MAX_BACKOFF_MS,
      queryKey: `run:${runId}`,
      auth,
      enabled: runId !== '' && settledRunId !== runId,
    }
  );
  const { data, lastUpdatedAt, refetch } = query;
  const terminal =
    data !== null && data.run_id === runId && isTerminal(data.status);

  useEffect(() => {
    if (terminal) {
      setSettledRunId(runId);
    }
  }, [terminal, runId]);

  useEffect(() => {
    if (runId === '' || terminal) {
      return;
    }
    const timer = setInterval(() => {
      if (!pageVisible()) {
        return;
      }
      const age =
        lastUpdatedAt === null ? Infinity : Date.now() - lastUpdatedAt;
      if (age > RUN_STALE_AFTER_MS) {
        void refetch();
      }
    }, RUN_POLL_INTERVAL_MS);
    return () => {
      clearInterval(timer);
    };
  }, [runId, terminal, lastUpdatedAt, refetch]);

  return query;
}
