/** The log viewer, tailing one phase of a run's CloudWatch stream. */

import { useCallback, useEffect, useRef, useState } from 'react';

import { api, type RunLogLine, type RunPhase } from '../api';
import { ErrorNotice } from './ErrorNotice';
import { Spinner } from './Spinner';

/** Props for {@link RunLogViewer}. */
export interface RunLogViewerProps {
  runId: string;
  phase: RunPhase;
  /** Whether to keep polling. False once the run's phase has finished. */
  live: boolean;
  /** Milliseconds between polls. Defaults to 3000. */
  intervalMs?: number;
}

/**
 * Tails one phase of a run's logs.
 *
 * Its own poll loop rather than `usePolledQuery`, because the route is a cursor
 * feed: each page carries `next_after` and the lines append, so a hook that
 * replaces `data` on every tick would throw away the transcript. The cursor
 * lives in a ref so advancing it cannot re-arm the timer.
 */
export function RunLogViewer({
  runId,
  phase,
  live,
  intervalMs = 3000,
}: RunLogViewerProps): React.ReactElement {
  const [lines, setLines] = useState<RunLogLine[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [complete, setComplete] = useState(false);
  const after = useRef<string | null>(null);
  const inFlight = useRef(false);

  useEffect(() => {
    setLines([]);
    setComplete(false);
    setLoading(true);
    after.current = null;
  }, [runId, phase]);

  const poll = useCallback(async (): Promise<void> => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    try {
      const page = await api.getRunLogs(runId, {
        phase,
        after: after.current,
      });
      after.current = page.next_after ?? after.current;
      if (page.lines.length > 0) {
        setLines((previous) => [...previous, ...page.lines]);
      }
      setComplete(page.complete);
      setError(null);
    } catch (thrown) {
      setError(thrown);
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, [runId, phase]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async (): Promise<void> => {
      await poll();
      if (cancelled || !live) {
        return;
      }
      timer = setTimeout(() => {
        void tick();
      }, intervalMs);
    };
    void tick();

    return () => {
      cancelled = true;
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    };
  }, [poll, live, intervalMs]);

  return (
    <div className="space-y-2">
      <ErrorNotice error={error} />
      <pre
        data-testid="run-logs"
        className="max-h-96 overflow-auto rounded-md bg-surface-900 p-3 font-mono text-xs text-surface-200"
      >
        {lines.map((line) => line.message).join('\n')}
      </pre>
      {loading ? <Spinner label="Loading the logs" /> : null}
      {complete ? (
        <p className="text-xs text-surface-400">End of the {phase} log.</p>
      ) : null}
    </div>
  );
}
