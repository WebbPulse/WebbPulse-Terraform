/** Runs a GitHub callback's API call exactly once, since each state works once. */

import { useEffect, useRef, useState } from 'react';

/** Where a one-shot callback call stands. */
export interface CallbackResult<T> {
  data: T | null;
  error: unknown;
  isLoading: boolean;
}

/**
 * Calls `run` once per mount and holds the outcome.
 *
 * The ref guard matters: the state GitHub echoes back is single use, so the
 * second effect run React's strict mode makes in development would spend it and
 * fail. `run` returning null means there is nothing to call.
 */
export function useCallbackOnce<T>(
  run: () => Promise<T> | null
): CallbackResult<T> {
  const started = useRef(false);
  const [result, setResult] = useState<CallbackResult<T>>({
    data: null,
    error: null,
    isLoading: true,
  });
  useEffect(() => {
    if (started.current) {
      return;
    }
    started.current = true;
    const pending = run();
    if (pending === null) {
      setResult({ data: null, error: null, isLoading: false });
      return;
    }
    pending.then(
      (data) => {
        setResult({ data, error: null, isLoading: false });
      },
      (error: unknown) => {
        setResult({ data: null, error, isLoading: false });
      }
    );
  }, [run]);
  return result;
}
