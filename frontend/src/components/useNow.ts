/** A clock that re-renders on a fixed interval while something is still moving. */

import { useEffect, useState } from 'react';

/** The current time, refreshed every `intervalMs` while `ticking`, frozen otherwise. */
export function useNow(ticking: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!ticking) {
      return;
    }
    setNow(Date.now());
    const timer = setInterval(() => {
      setNow(Date.now());
    }, intervalMs);
    return () => {
      clearInterval(timer);
    };
  }, [ticking, intervalMs]);

  return now;
}
