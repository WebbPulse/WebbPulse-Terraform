/** Load the complete history required by the existing run views. */

import { api } from './client';
import type { Run, RunList, RunListQuery } from './types';

/** Follow cursors serially, preserving cancellation and rejecting cursor loops. */
export async function listRunHistory(
  query: Pick<RunListQuery, 'workspace_id'>,
  signal: AbortSignal
): Promise<RunList> {
  const items = new Map<string, Run>();
  const seen = new Set<string>();
  let cursor: string | undefined;
  do {
    signal.throwIfAborted();
    const page = await api.listRuns(
      { ...query, ...(cursor === undefined ? {} : { cursor }) },
      { signal }
    );
    for (const run of page.items) items.set(run.run_id, run);
    cursor = page.next_cursor ?? undefined;
    if (cursor !== undefined) {
      if (seen.has(cursor)) throw new Error('Run pagination did not advance.');
      seen.add(cursor);
    }
  } while (cursor !== undefined);
  return { items: [...items.values()], next_cursor: null };
}
