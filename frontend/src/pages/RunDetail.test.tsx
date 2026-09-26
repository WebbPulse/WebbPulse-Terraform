import { cleanup, screen, waitFor, within } from '@testing-library/react';
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
import type { Run, RunState } from '../api';

vi.mock('../api/client', () => apiClientModuleMock());

const fetchRunPlan = vi.fn();
vi.mock('../api/runPlan', () => ({
  fetchRunPlan: (...args: unknown[]) =>
    (fetchRunPlan as (...a: unknown[]) => unknown)(...args),
}));

const { RunDetail } = await import('./RunDetail');

/** A run a push or a pull request started through the GitHub App. */
function aVcsRun(
  status: RunState,
  vcs: Partial<NonNullable<Run['vcs']>> = {},
  overrides: Partial<Run> = {}
): Run {
  return aRun(status, {
    message: '',
    source: vcs.pr_number === undefined ? 'vcs_push' : 'vcs_pr',
    vcs: {
      repo: 'WebbPulse/infra',
      repository_id: '42',
      ref: 'refs/heads/staging',
      branch: 'staging',
      sha: 'abcdef1234567890abcdef1234567890abcdef12',
      commit_message: 'Add the logs bucket\n\nLonger body.',
      ...vcs,
    },
    ...overrides,
  });
}

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

  it('asks for an optional comment before confirming, in an in-app dialog', async () => {
    const nativeConfirm = vi.spyOn(window, 'confirm');
    apiMock.getRun.mockResolvedValue(aRun('awaiting_confirmation'));
    apiMock.confirmRun.mockResolvedValue(aRun('applying'));

    renderRun();

    await userEvent.click(
      await screen.findByRole('button', { name: 'Confirm & apply' })
    );
    const dialog = screen.getByRole('dialog', { name: 'Confirm & apply' });
    expect(apiMock.confirmRun).not.toHaveBeenCalled();
    await userEvent.type(
      within(dialog).getByLabelText('Comment'),
      'Reviewed with the team.'
    );
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Confirm plan' })
    );

    await waitFor(() => {
      expect(apiMock.confirmRun).toHaveBeenCalledWith(
        'run-01J000000000000000000000',
        'Reviewed with the team.'
      );
    });
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
    expect(nativeConfirm).not.toHaveBeenCalled();
    nativeConfirm.mockRestore();
  });

  it('closes the confirm dialog without confirming', async () => {
    apiMock.getRun.mockResolvedValue(aRun('awaiting_confirmation'));

    renderRun();

    await userEvent.click(
      await screen.findByRole('button', { name: 'Confirm & apply' })
    );
    const dialog = screen.getByRole('dialog', { name: 'Confirm & apply' });
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Cancel' })
    );

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(apiMock.confirmRun).not.toHaveBeenCalled();
  });

  it('discards through a dialog with an optional comment', async () => {
    apiMock.getRun.mockResolvedValue(aRun('awaiting_confirmation'));
    apiMock.discardRun.mockResolvedValue(aRun('discarded'));

    renderRun();

    await userEvent.click(
      await screen.findByRole('button', { name: 'Discard run' })
    );
    const dialog = screen.getByRole('dialog', { name: 'Discard run' });
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Discard run' })
    );

    await waitFor(() => {
      expect(apiMock.discardRun).toHaveBeenCalledWith(
        'run-01J000000000000000000000',
        ''
      );
    });
  });

  it('keeps the dialog open with the error when the confirm is refused', async () => {
    apiMock.getRun.mockResolvedValue(aRun('awaiting_confirmation'));
    apiMock.confirmRun.mockRejectedValue(new Error('The run moved on.'));

    renderRun();

    await userEvent.click(
      await screen.findByRole('button', { name: 'Confirm & apply' })
    );
    const dialog = screen.getByRole('dialog', { name: 'Confirm & apply' });
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Confirm plan' })
    );

    expect(
      await within(dialog).findByText('The run moved on.')
    ).toBeInTheDocument();
  });

  it('shows who confirmed the plan and their comment in the timeline', async () => {
    apiMock.getRun.mockResolvedValue(
      aRun('applying', {
        decision: {
          action: 'confirmed',
          at: '2026-09-17T00:05:00Z',
          actor: { kind: 'user', id: 'user-1', display_name: 'Tyler Webb' },
          comment: 'Reviewed with the team.',
        },
      })
    );

    renderRun();

    const note = await screen.findByTestId('run-decision');
    expect(note).toHaveTextContent('Confirmed by Tyler Webb');
    expect(
      within(screen.getByTestId('run-timeline')).getByTestId(
        'run-decision-comment'
      )
    ).toHaveTextContent('Reviewed with the team.');
  });

  it('shows a discard without a comment', async () => {
    apiMock.getRun.mockResolvedValue(
      aRun('discarded', {
        decision: {
          action: 'discarded',
          at: '2026-09-17T00:05:00Z',
          actor: { kind: 'user', id: 'user-1' },
          comment: null,
        },
      })
    );

    renderRun();

    expect(await screen.findByTestId('run-decision')).toHaveTextContent(
      'Discarded by user-1'
    );
    expect(
      screen.queryByTestId('run-decision-comment')
    ).not.toBeInTheDocument();
  });

  it('shows the trigger, the commit and its message for a push run', async () => {
    apiMock.getRun.mockResolvedValue(aVcsRun('applied'));

    renderRun();

    const source = await screen.findByTestId('run-source');
    expect(source).toHaveTextContent(
      'Triggered via GitHub from a push to staging'
    );
    expect(
      within(source).getByRole('link', { name: 'Commit abcdef1 on GitHub' })
    ).toHaveAttribute(
      'href',
      'https://github.com/WebbPulse/infra/commit/abcdef1234567890abcdef1234567890abcdef12'
    );
    expect(within(source).queryByText(/PR #/)).not.toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'Add the logs bucket' })
    ).toBeInTheDocument();
  });

  it('links the pull request and its head commit for a pull request run', async () => {
    apiMock.getRun.mockResolvedValue(
      aVcsRun(
        'planned_and_finished',
        {
          pr_number: 91,
          ref: 'refs/pull/91/merge',
          branch: null,
          sha: 'merge00000000000000000000000000000000000',
          head_sha: '1234567aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        },
        { message: 'Queued from CI' }
      )
    );

    renderRun();

    const source = await screen.findByTestId('run-source');
    expect(source).toHaveTextContent(
      'Triggered via GitHub from pull request #91'
    );
    expect(
      within(source).getByRole('link', { name: 'PR #91' })
    ).toHaveAttribute('href', 'https://github.com/WebbPulse/infra/pull/91');
    expect(
      within(source).getByRole('link', { name: 'Commit 1234567 on GitHub' })
    ).toBeInTheDocument();
    expect(screen.getByTestId('run-commit-message')).toHaveTextContent(
      'Add the logs bucket'
    );
  });

  it('shows no source line for a run started by hand', async () => {
    apiMock.getRun.mockResolvedValue(aRun('applied'));

    renderRun();

    await screen.findByTestId('run-state-badge');
    expect(screen.queryByTestId('run-source')).not.toBeInTheDocument();
  });
});
