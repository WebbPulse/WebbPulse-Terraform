import { beforeEach, describe, expect, it, vi } from 'vitest';

import { aRun } from '../test-helpers/fixtures';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../test-helpers/apiMock';
import { listRunHistory } from './runHistory';

vi.mock('./client', () => apiClientModuleMock());

describe('listRunHistory', () => {
  beforeEach(resetApiMock);

  it.each([{}, { workspace_id: 'ws-1' }])(
    'reads all pages for %j',
    async (query) => {
      apiMock.listRuns
        .mockResolvedValueOnce({
          items: [aRun('applied')],
          next_cursor: 'page-2',
        })
        .mockResolvedValueOnce({
          items: [aRun('errored', { run_id: 'older' })],
          next_cursor: null,
        });
      const signal = new AbortController().signal;
      const result = await listRunHistory(query, signal);
      expect(result.items).toHaveLength(2);
      expect(apiMock.listRuns).toHaveBeenNthCalledWith(
        2,
        { ...query, cursor: 'page-2' },
        { signal }
      );
    }
  );

  it('stops on a repeated cursor', async () => {
    apiMock.listRuns.mockResolvedValue({ items: [], next_cursor: 'same' });
    await expect(
      listRunHistory({}, new AbortController().signal)
    ).rejects.toThrow('did not advance');
    expect(apiMock.listRuns).toHaveBeenCalledTimes(2);
  });

  it('does not request a page after cancellation', async () => {
    const controller = new AbortController();
    controller.abort();
    await expect(listRunHistory({}, controller.signal)).rejects.toThrow();
    expect(apiMock.listRuns).not.toHaveBeenCalled();
  });
});
