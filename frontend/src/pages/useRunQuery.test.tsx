import { act, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { aRun } from '../test-helpers/fixtures';
import {
  renderWithAuth,
  signedInAuthClient,
} from '../test-helpers/renderWithAuth';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../test-helpers/apiMock';

vi.mock('../api/client', () => apiClientModuleMock());

const { RUN_POLL_INTERVAL_MS, RUN_READ_DEADLINE_MS, useRunQuery } =
  await import('./useRunQuery');

const RUN = 'run-01J000000000000000000000';

/** Prints the polled run's status, so a test can watch it move. */
function Probe(): React.ReactElement {
  const query = useRunQuery(RUN);
  return <output data-testid="status">{query.data?.status ?? 'none'}</output>;
}

/** Lets the poll's timers run forward by `ms`. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe('useRunQuery', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    resetApiMock();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('follows the run through every phase and stops once it is terminal', async () => {
    const states = [
      'planning',
      'awaiting_confirmation',
      'applying',
      'applied',
    ] as const;
    let call = 0;
    apiMock.getRun.mockImplementation(() =>
      Promise.resolve(aRun(states[Math.min(call++, states.length - 1)]))
    );

    renderWithAuth(<Probe />, signedInAuthClient());

    for (const state of states) {
      await waitFor(() => {
        expect(screen.getByTestId('status')).toHaveTextContent(state);
      });
      await advance(RUN_POLL_INTERVAL_MS);
    }

    const settledCalls = apiMock.getRun.mock.calls.length;
    await advance(RUN_POLL_INTERVAL_MS * 10);
    expect(apiMock.getRun.mock.calls.length).toBe(settledCalls);
  });

  it('abandons a read that never settles and polls again', async () => {
    let hungSignal: AbortSignal | undefined;
    apiMock.getRun
      .mockImplementationOnce(() => Promise.resolve(aRun('planning')))
      .mockImplementationOnce(
        (_runId: string, init?: { signal?: AbortSignal }) => {
          hungSignal = init?.signal;
          return new Promise(() => undefined);
        }
      )
      .mockImplementation(() => Promise.resolve(aRun('applied')));

    renderWithAuth(<Probe />, signedInAuthClient());

    await waitFor(() => {
      expect(screen.getByTestId('status')).toHaveTextContent('planning');
    });
    await advance(RUN_POLL_INTERVAL_MS);
    await advance(RUN_READ_DEADLINE_MS + RUN_POLL_INTERVAL_MS * 6);

    await waitFor(() => {
      expect(screen.getByTestId('status')).toHaveTextContent('applied');
    });
    expect(hungSignal?.aborted).toBe(true);
  });
});
