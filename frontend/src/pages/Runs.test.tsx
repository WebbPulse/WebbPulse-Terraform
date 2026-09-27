import { screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { aRun, aWorkspace } from '../test-helpers/fixtures';
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

const { Runs } = await import('./Runs');

describe('Runs', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.listWorkspaces.mockResolvedValue({ items: [aWorkspace()] });
  });

  it('lists every run with its state and plan counts', async () => {
    apiMock.listRuns.mockResolvedValue({
      items: [
        aRun('applied'),
        aRun('errored', { run_id: 'run-2', changes: null }),
      ],
    });

    renderWithAuth(<Runs />, signedInAuthClient());

    const badges = await screen.findAllByTestId('run-state-badge');
    expect(badges[0]).toHaveAttribute('data-state', 'applied');
    expect(badges[1]).toHaveAttribute('data-state', 'errored');
    expect(apiMock.listRuns).toHaveBeenCalledTimes(1);
    expect(apiMock.listRuns.mock.calls[0]?.[0]).not.toHaveProperty(
      'workspace_id'
    );
    expect(
      screen.getAllByLabelText('3 to add, 1 to change, 0 to destroy').length
    ).toBeGreaterThan(0);
  });

  it('marks a destroy run in the list', async () => {
    apiMock.listRuns.mockResolvedValue({
      items: [
        aRun('awaiting_confirmation', { is_destroy: true }),
        aRun('applied', { run_id: 'run-2' }),
      ],
    });

    renderWithAuth(<Runs />, signedInAuthClient());

    const rows = await screen.findAllByRole('listitem');
    expect(
      within(rows[0] as HTMLElement).getByTestId('destroy-badge')
    ).toBeInTheDocument();
    expect(rows[0]).toHaveTextContent('destroy run');
    expect(
      within(rows[1] as HTMLElement).queryByTestId('destroy-badge')
    ).not.toBeInTheDocument();
    expect(rows[1]).toHaveTextContent('plan and apply run');
  });

  it('says so when there are no runs', async () => {
    apiMock.listRuns.mockResolvedValue({ items: [] });

    renderWithAuth(<Runs />, signedInAuthClient());

    expect(await screen.findByText('No runs yet.')).toBeInTheDocument();
  });

  it('shows the trigger, the short sha and the pull request of a VCS run', async () => {
    apiMock.listRuns.mockResolvedValue({
      items: [
        aRun('planned_and_finished', {
          message: '',
          source: 'vcs_pr',
          vcs: {
            repo: 'WebbPulse/infra',
            repository_id: '42',
            ref: 'refs/pull/91/merge',
            sha: 'merge00000000000000000000000000000000000',
            head_sha: '1234567aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            pr_number: 91,
            commit_message: 'Add the logs bucket',
          },
        }),
      ],
    });

    renderWithAuth(<Runs />, signedInAuthClient());

    const source = await screen.findByTestId('run-source');
    expect(source).toHaveTextContent(
      'Triggered via GitHub from pull request #91'
    );
    expect(
      within(source).getByRole('link', { name: 'Commit 1234567 on GitHub' })
    ).toHaveAttribute(
      'href',
      'https://github.com/WebbPulse/infra/commit/1234567aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    );
    expect(
      within(source).getByRole('link', { name: 'PR #91' })
    ).toHaveAttribute('href', 'https://github.com/WebbPulse/infra/pull/91');
    expect(
      screen.getByRole('link', { name: 'Add the logs bucket' })
    ).toBeInTheDocument();
  });

  it('titles a push run by its commit and links the pull request it merged', async () => {
    apiMock.listRuns.mockResolvedValue({
      items: [
        aRun('applied', {
          message: 'Push of abcdef1 to main',
          source: 'vcs_push',
          vcs: {
            repo: 'WebbPulse/infra',
            repository_id: '42',
            ref: 'refs/heads/main',
            branch: 'main',
            sha: 'abcdef1234567890abcdef1234567890abcdef12',
            commit_message: 'Lengthen the pet (#12)\n\nBody.',
            pull_request: {
              number: 12,
              url: 'https://github.com/WebbPulse/infra/pull/12',
            },
          },
        }),
      ],
    });

    renderWithAuth(<Runs />, signedInAuthClient());

    expect(
      await screen.findByRole('link', { name: 'Lengthen the pet (#12)' })
    ).toBeInTheDocument();
    expect(
      screen.queryByText('Push of abcdef1 to main')
    ).not.toBeInTheDocument();
    const source = screen.getByTestId('run-source');
    expect(source).toHaveTextContent(
      'Triggered via GitHub from a push to main'
    );
    expect(
      within(source).getByRole('link', { name: 'PR #12' })
    ).toHaveAttribute('href', 'https://github.com/WebbPulse/infra/pull/12');
  });
});
