import { beforeEach, describe, expect, it, vi } from 'vitest';

import { aRun } from '../test-helpers/fixtures';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../test-helpers/apiMock';
import { listRunHistory, RUN_HISTORY_PAGE_SIZE } from './runHistory';

vi.mock('./client', () => apiClientModuleMock());

describe('listRunHistory', () => {
  beforeEach(resetApiMock);

  it('reads every page of the cross-workspace list in order', async () => {
    apiMock.listRuns
      .mockResolvedValueOnce({
        items: [aRun('applied', { run_id: 'run-2' })],
        next_cursor: 'run-2',
      })
      .mockResolvedValueOnce({
        items: [aRun('errored', { run_id: 'run-1' })],
        next_cursor: null,
      });
    const signal = new AbortController().signal;

    const runs = await listRunHistory(signal);

    expect(runs.map((run) => run.run_id)).toEqual(['run-2', 'run-1']);
    expect(apiMock.listRuns).toHaveBeenNthCalledWith(
      1,
      { limit: RUN_HISTORY_PAGE_SIZE },
      { signal }
    );
    expect(apiMock.listRuns).toHaveBeenNthCalledWith(
      2,
      { limit: RUN_HISTORY_PAGE_SIZE, cursor: 'run-2' },
      { signal }
    );
  });

  it('stops on a repeated cursor', async () => {
    apiMock.listRuns.mockResolvedValue({ items: [], next_cursor: 'run-9' });
    await expect(listRunHistory(new AbortController().signal)).rejects.toThrow(
      'did not advance'
    );
    expect(apiMock.listRuns).toHaveBeenCalledTimes(2);
  });

  it('does not request a page after cancellation', async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(listRunHistory(controller.signal)).rejects.toThrow();
    expect(apiMock.listRuns).not.toHaveBeenCalled();
  });
});
