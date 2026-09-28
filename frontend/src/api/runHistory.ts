/** Every run in the environment, read from the cross-workspace list. */

import { api } from './client';
import type { Run } from './types';

/** The largest page the backend serves, so the page takes as few requests as it can. */
export const RUN_HISTORY_PAGE_SIZE = 200;

/**
 * Every run across every workspace, newest first.
 *
 * Follows `next_cursor` to the end one page at a time, stops on cancellation and
 * refuses a cursor it has already followed rather than looping.
 */
export async function listRunHistory(signal: AbortSignal): Promise<Run[]> {
  const items: Run[] = [];
  const seen = new Set<string>();
  let cursor: string | undefined;
  do {
    signal.throwIfAborted();
    const page = await api.listRuns(
      {
        limit: RUN_HISTORY_PAGE_SIZE,
        ...(cursor === undefined ? {} : { cursor }),
      },
      { signal }
    );
    items.push(...page.items);
    cursor = page.next_cursor ?? undefined;
    if (cursor !== undefined) {
      if (seen.has(cursor)) throw new Error('Run pagination did not advance.');
      seen.add(cursor);
    }
  } while (cursor !== undefined);
  return items;
}
