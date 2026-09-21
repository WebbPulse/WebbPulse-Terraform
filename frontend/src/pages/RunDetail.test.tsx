import { cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

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
import type { RunState } from '../api';

vi.mock('../api/client', () => apiClientModuleMock());

const { RunDetail } = await import('./RunDetail');

/** Mounts the run detail page on a route carrying the run id. */
function renderRun(): void {
  renderWithAuth(
    <Routes>
      <Route path="/runs/:runId" element={<RunDetail />} />
    </Routes>,
    signedInAuthClient(),
    ['/runs/run-01J000000000000000000000']
  );
}

describe('RunDetail', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getRunLogs.mockResolvedValue({
      run_id: 'run-1',
      phase: 'plan',
      events: [{ timestamp: 1, message: 'Plan: 3 to add, 1 to change.' }],
      next_after: 'tok-1',
    });
  });

  it('renders the state badge, the plan counts and the log tail', async () => {
    apiMock.getRun.mockResolvedValue(aRun('awaiting_confirmation'));

    renderRun();

    const badge = await screen.findByTestId('run-state-badge');
    expect(badge).toHaveAttribute('data-state', 'awaiting_confirmation');
    expect(badge).toHaveTextContent('Needs confirmation');

    const summary = screen.getByTestId('plan-summary');
    expect(summary).toHaveTextContent('3');
    expect(summary).toHaveTextContent('1');

    await waitFor(() => {
      expect(screen.getByTestId('run-logs')).toHaveTextContent(
        'Plan: 3 to add, 1 to change.'
      );
    });
  });

  it('offers confirm and discard on a run awaiting confirmation, not cancel', async () => {
    apiMock.getRun.mockResolvedValue(aRun('awaiting_confirmation'));

    renderRun();

    expect(
      await screen.findByRole('button', { name: 'Confirm & apply' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Discard run' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Cancel run' })
    ).not.toBeInTheDocument();
  });

  it('offers discard but not cancel on a planned run', async () => {
    apiMock.getRun.mockResolvedValue(aRun('planned'));

    renderRun();

    expect(
      await screen.findByRole('button', { name: 'Discard run' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Cancel run' })
    ).not.toBeInTheDocument();
  });

  it('offers only cancel while a run is planning', async () => {
    apiMock.getRun.mockResolvedValue(aRun('planning'));

    renderRun();

    expect(
      await screen.findByRole('button', { name: 'Cancel run' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Confirm & apply' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Discard run' })
    ).not.toBeInTheDocument();
  });

  it('withholds confirm from a plan only run that is awaiting confirmation', async () => {
    apiMock.getRun.mockResolvedValue(
      aRun('awaiting_confirmation', { plan_only: true })
    );

    renderRun();

    expect(
      await screen.findByRole('button', { name: 'Discard run' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Confirm & apply' })
    ).not.toBeInTheDocument();
  });

  it('offers no action on a terminal run', async () => {
    for (const state of ['applied', 'errored', 'discarded'] as RunState[]) {
      cleanup();
      apiMock.getRun.mockResolvedValue(aRun(state));
      renderRun();

      await screen.findByTestId('run-state-badge');
      expect(
        screen.queryByRole('button', { name: 'Confirm & apply' })
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: 'Cancel run' })
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: 'Discard run' })
      ).not.toBeInTheDocument();
    }
  });

  it('reads the plan log first and keeps the apply log closed before an apply', async () => {
    apiMock.getRun.mockResolvedValue(aRun('awaiting_confirmation'));

    renderRun();

    await screen.findByTestId('run-state-badge');
    await waitFor(() => {
      expect(apiMock.getRunLogs).toHaveBeenCalledWith(
        'run-01J000000000000000000000',
        {
          phase: 'plan',
          after: null,
        }
      );
    });
    expect(apiMock.getRunLogs).not.toHaveBeenCalledWith(
      'run-01J000000000000000000000',
      expect.objectContaining({ phase: 'apply' })
    );
    expect(
      screen.getByRole('button', { name: /Plan finished/ })
    ).toHaveAttribute('aria-expanded', 'true');
    expect(
      screen.getByRole('button', { name: /Apply pending/ })
    ).toHaveAttribute('aria-expanded', 'true');
  });

  it('opens the apply section on its log once the run is applying', async () => {
    apiMock.getRun.mockResolvedValue(aRun('applying'));

    renderRun();

    await screen.findByTestId('run-state-badge');
    await waitFor(() => {
      expect(apiMock.getRunLogs).toHaveBeenCalledWith(
        'run-01J000000000000000000000',
        {
          phase: 'apply',
          after: null,
        }
      );
    });
    expect(
      screen.getByRole('button', { name: /Apply running/ })
    ).toHaveAttribute('aria-expanded', 'true');
    expect(
      screen.getByRole('button', { name: /Plan finished/ })
    ).toHaveAttribute('aria-expanded', 'false');
  });

  it('shows the elapsed time and the run details on request', async () => {
    apiMock.getRun.mockResolvedValue(aRun('applied'));

    renderRun();

    await screen.findByTestId('run-state-badge');
    expect(screen.getByText('Plan and apply duration')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Run details/ }));
    expect(screen.getByText('cv-01J000000000000000000000')).toBeInTheDocument();
  });
});
