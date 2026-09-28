/** One clock shared by every relative timestamp, ticking only while something reads it. */

import { useSyncExternalStore } from 'react';

/** How often the shared clock advances while anything reads it. */
export const RELATIVE_TIME_TICK_MS = 5000;

const listeners = new Set<() => void>();
let now = Date.now();
let timer: ReturnType<typeof setInterval> | undefined;

/** Advances the clock and tells every reader. */
function tick(): void {
  now = Date.now();
  for (const listener of [...listeners]) {
    listener();
  }
}

/** Catches the clock up when the tab returns, since hidden tabs throttle timers. */
function onVisible(): void {
  if (document.visibilityState === 'visible') {
    tick();
  }
}

/**
 * Subscribes a reader, starting the one interval on the first and stopping it
 * after the last, so an idle page runs no timer at all.
 */
function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === undefined) {
    now = Date.now();
    timer = setInterval(tick, RELATIVE_TIME_TICK_MS);
    document.addEventListener('visibilitychange', onVisible);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== undefined) {
      clearInterval(timer);
      timer = undefined;
      document.removeEventListener('visibilitychange', onVisible);
    }
  };
}

/** The shared clock's current reading. */
function snapshot(): number {
  return now;
}

/** The current time from the shared clock, re-rendering the caller as it ticks. */
export function useClock(): number {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}
