/** The log viewer, tailing one phase of a run's CloudWatch stream. */

import { useCallback, useEffect, useRef, useState } from 'react';

import { api, type RunLogLine, type RunPhase } from '../api';
import { Button } from './Button';
import { CopyButton } from './CopyButton';
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
  /** Controls to show in the bar beside the viewer's own, such as phase tabs. */
  controls?: React.ReactNode;
}

/**
 * Tails one phase of a run's logs.
 *
 * Its own poll loop rather than `usePolledQuery`, because the route is a cursor
 * feed: each page carries `next_after` and the lines append, so a hook that
 * replaces `data` on every tick would throw away the transcript. The cursor
 * lives in a ref so advancing it cannot re-arm the timer.
 *
 * The viewer follows the tail while `live`, and stops following the moment the
 * person scrolls up, so reading an earlier line is not fought by the poll.
 */
export function RunLogViewer({
  runId,
  phase,
  live,
  intervalMs = 3000,
  controls,
}: RunLogViewerProps): React.ReactElement {
  const [lines, setLines] = useState<RunLogLine[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [follow, setFollow] = useState(true);
  const [wrap, setWrap] = useState(false);
  const after = useRef<string | null>(null);
  const inFlight = useRef(false);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLines([]);
    setLoading(true);
    setFollow(true);
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
      if (page.events.length > 0) {
        setLines((previous) => [...previous, ...page.events]);
      }
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

  useEffect(() => {
    if (follow && scroller.current !== null) {
      scroller.current.scrollTop = scroller.current.scrollHeight;
    }
  }, [lines, follow]);

  const onScroll = (): void => {
    const element = scroller.current;
    if (element === null) {
      return;
    }
    const atBottom =
      element.scrollHeight - element.scrollTop - element.clientHeight < 8;
    if (atBottom !== follow) {
      setFollow(atBottom);
    }
  };

  const text = lines.map((line) => line.message).join('\n');

  return (
    <div className="overflow-hidden rounded-lg border border-line">
      <div className="sticky top-0 z-10 flex flex-wrap items-center justify-between gap-2 border-b border-line bg-panel px-2 py-1.5">
        <div className="flex items-center gap-2">{controls}</div>
        <div className="flex items-center gap-1 text-xs text-text-faint">
          <span className="px-1 tabular-nums">
            {lines.length} {lines.length === 1 ? 'line' : 'lines'}
          </span>
          {live ? (
            <span className="inline-flex items-center gap-1 px-1 text-brand-300">
              <span
                aria-hidden="true"
                className="size-1.5 animate-pulse rounded-full bg-brand-400"
              />
              Live
            </span>
          ) : null}
          <Button
            size="sm"
            variant="ghost"
            aria-pressed={wrap}
            onClick={() => {
              setWrap((value) => !value);
            }}
          >
            Wrap
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-pressed={follow}
            onClick={() => {
              setFollow(true);
              if (scroller.current !== null) {
                scroller.current.scrollTop = scroller.current.scrollHeight;
              }
            }}
          >
            Follow
          </Button>
          <CopyButton value={text} subject="log" disabled={text === ''} />
        </div>
      </div>
      <ErrorNotice error={error} className="m-2" />
      <div
        ref={scroller}
        onScroll={onScroll}
        className="max-h-[60vh] min-h-48 overflow-auto bg-bg"
      >
        <pre
          data-testid="run-logs"
          className={`p-3 font-mono text-xs leading-relaxed text-surface-200 ${
            wrap ? 'whitespace-pre-wrap' : ''
          }`}
          style={{ counterReset: 'log' }}
        >
          {lines.map((line, index) => (
            <span
              key={`${String(line.timestamp)}-${String(index)}`}
              className="log-line block"
            >
              {line.message}
              {'\n'}
            </span>
          ))}
        </pre>
        {loading ? (
          <div className="flex items-center gap-2 px-3 pb-3 text-xs text-text-faint">
            <Spinner label="Loading the logs" className="size-3.5" />
            Loading the logs
          </div>
        ) : lines.length === 0 ? (
          <p className="px-3 pb-3 text-xs text-text-faint">
            {live ? 'Waiting for output.' : 'No output was recorded.'}
          </p>
        ) : null}
      </div>
    </div>
  );
}
