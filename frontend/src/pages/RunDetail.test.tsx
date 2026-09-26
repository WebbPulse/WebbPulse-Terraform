import { cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { aRun, aRunPlan } from '../test-helpers/fixtures';
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

const fetchRunPlan = vi.fn();
vi.mock('../api/runPlan', () => ({
  fetchRunPlan: (...args: unknown[]) =>
    (fetchRunPlan as (...a: unknown[]) => unknown)(...args),
}));

const { RunDetail } = await import('./RunDetail');

/** Switches the body to the raw log tab. */
async function openRawLog(): Promise<void> {
  await userEvent.click(screen.getByRole('tab', { name: 'Raw log' }));
}

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
    fetchRunPlan.mockReset();
    fetchRunPlan.mockResolvedValue(aRunPlan());
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

    const summary = await screen.findByTestId('plan-summary-line');
    expect(summary).toHaveTextContent('2 to add');
    expect(summary).toHaveTextContent('1 to change');
    expect(summary).toHaveTextContent('1 to destroy');
    expect(summary).toHaveTextContent('1 to replace');

    await openRawLog();
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

  it('labels a destroy run and asks to confirm the destroy', async () => {
    apiMock.getRun.mockResolvedValue(
      aRun('awaiting_confirmation', { is_destroy: true })
    );

    renderRun();

    expect(
      await screen.findByRole('button', { name: 'Confirm & destroy' })
    ).toBeInTheDocument();
    expect(screen.getByTestId('destroy-badge')).toBeInTheDocument();
    expect(screen.getByText(/Destroy run triggered/)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Confirm & apply' })
    ).not.toBeInTheDocument();
  });

  it('withholds confirm from a plan only destroy run', async () => {
    apiMock.getRun.mockResolvedValue(
      aRun('planned_and_finished', { is_destroy: true, plan_only: true })
    );

    renderRun();

    expect(await screen.findByTestId('destroy-badge')).toBeInTheDocument();
    expect(
      screen.getByText(/Plan only destroy run triggered/)
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Confirm & destroy' })
    ).not.toBeInTheDocument();
  });

  it('shows no destroy badge on an ordinary run', async () => {
    apiMock.getRun.mockResolvedValue(aRun('applied'));

    renderRun();

    await screen.findByTestId('run-state-badge');
    expect(screen.queryByTestId('destroy-badge')).not.toBeInTheDocument();
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
    await openRawLog();
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
  });

  it('opens the apply section on its log once the run is applying', async () => {
    apiMock.getRun.mockResolvedValue(aRun('applying'));

    renderRun();

    await screen.findByTestId('run-state-badge');
    await openRawLog();
    await waitFor(() => {
      expect(apiMock.getRunLogs).toHaveBeenCalledWith(
        'run-01J000000000000000000000',
        {
          phase: 'apply',
          after: null,
        }
      );
    });
  });

  it('keeps the plan log readable once the run applied', async () => {
    apiMock.getRun.mockResolvedValue(aRun('applied'));

    renderRun();

    await screen.findByTestId('run-state-badge');
    await openRawLog();
    await waitFor(() => {
      expect(apiMock.getRunLogs).toHaveBeenCalledWith(
        'run-01J000000000000000000000',
        { phase: 'apply', after: null }
      );
    });
    await userEvent.click(screen.getByRole('tab', { name: 'Plan log' }));
    await waitFor(() => {
      expect(apiMock.getRunLogs).toHaveBeenCalledWith(
        'run-01J000000000000000000000',
        { phase: 'plan', after: null }
      );
    });
    expect(screen.getByTestId('run-log-phases')).toHaveAttribute(
      'data-phase',
      'plan'
    );
  });

  it('offers only the plan log before an apply', async () => {
    apiMock.getRun.mockResolvedValue(aRun('awaiting_confirmation'));

    renderRun();

    await screen.findByTestId('run-state-badge');
    await openRawLog();
    expect(
      await screen.findByRole('tab', { name: 'Plan log' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('tab', { name: 'Apply log' })
    ).not.toBeInTheDocument();
  });

  it('shows the apply summary under an applied plan', async () => {
    apiMock.getRun.mockResolvedValue(
      aRun('applied', { apply_changes: { add: 2, change: 1, destroy: 0 } })
    );

    renderRun();

    expect(await screen.findByTestId('apply-summary-line')).toHaveTextContent(
      'Apply complete! Resources: 2 added, 1 changed, 0 destroyed.'
    );
  });

  it('shows the duration and links the configuration version', async () => {
    apiMock.getRun.mockResolvedValue(aRun('applied'));

    renderRun();

    await screen.findByTestId('run-state-badge');
    expect(screen.getByText('Duration')).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'cv-01J000000000000000000000' })
    ).toBeInTheDocument();
  });

  it('walks the run through the stages of its timeline', async () => {
    apiMock.getRun.mockResolvedValue(aRun('applying'));

    renderRun();

    const timeline = await screen.findByTestId('run-timeline');
    const current = timeline.querySelector('[data-status="current"]');
    expect(current).toHaveAttribute('data-stage', 'applying');
    expect(
      timeline.querySelector('[data-stage="plan_finished"]')
    ).toHaveAttribute('data-status', 'done');
  });

  it('drops the apply stages from a plan only run', async () => {
    apiMock.getRun.mockResolvedValue(
      aRun('planned_and_finished', { plan_only: true })
    );

    renderRun();

    const timeline = await screen.findByTestId('run-timeline');
    expect(timeline.querySelector('[data-stage="applying"]')).toBeNull();
    expect(timeline.querySelector('[data-stage="planning"]')).not.toBeNull();
  });

  it('renders the plan rather than the log by default', async () => {
    apiMock.getRun.mockResolvedValue(aRun('applied'));

    renderRun();

    expect(await screen.findByTestId('plan-view')).toBeInTheDocument();
    expect(screen.queryByTestId('run-logs')).not.toBeInTheDocument();
  });

  it('falls back to a notice when the plan cannot be read', async () => {
    apiMock.getRun.mockResolvedValue(aRun('applied'));
    fetchRunPlan.mockRejectedValue(new Error('The plan could not be read.'));

    renderRun();

    await screen.findByTestId('run-state-badge');
    await waitFor(() => {
      expect(
        screen.getByText(/structured plan could not be read/)
      ).toBeInTheDocument();
    });
  });

  it('says the plan is still running before it finishes', async () => {
    apiMock.getRun.mockResolvedValue(aRun('planning'));

    renderRun();

    expect(await screen.findByTestId('plan-pending')).toHaveTextContent(
      'The plan is running'
    );
    expect(fetchRunPlan).not.toHaveBeenCalled();
  });
});
