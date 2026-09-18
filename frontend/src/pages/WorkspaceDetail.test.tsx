import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import {
  aConfigVersion,
  aRun,
  aVariable,
  aWorkspace,
} from '../test-helpers/fixtures';
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

const { WorkspaceDetail } = await import('./WorkspaceDetail');

/** Mounts the detail page on a route carrying the workspace id. */
function renderDetail(): void {
  renderWithAuth(
    <Routes>
      <Route path="/workspaces/:workspaceId" element={<WorkspaceDetail />} />
    </Routes>,
    signedInAuthClient(),
    ['/workspaces/ws-01J000000000000000000000']
  );
}

describe('WorkspaceDetail', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.listVariables.mockResolvedValue({
      variables: [
        aVariable(),
        aVariable({ key: 'token', sensitive: true, value: null }),
      ],
    });
    apiMock.listConfigVersions.mockResolvedValue({
      config_versions: [aConfigVersion()],
    });
    apiMock.listRuns.mockResolvedValue({ runs: [aRun('planned')] });
  });

  it('opens on the overview tab with the workspace settings', async () => {
    renderDetail();

    expect(
      await screen.findByRole('heading', { name: 'platform' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('form', { name: 'Workspace settings' })
    ).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
  });

  it('shows all four tabs', async () => {
    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });
    for (const label of [
      'Overview',
      'Variables',
      'Configuration versions',
      'Runs',
    ]) {
      expect(screen.getByRole('tab', { name: label })).toBeInTheDocument();
    }
  });

  it('shows a sensitive variable as write only rather than its value', async () => {
    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });
    await userEvent.click(screen.getByRole('tab', { name: 'Variables' }));

    expect(await screen.findByText('Write only')).toBeInTheDocument();
    expect(screen.getByText('us-west-2')).toBeInTheDocument();
  });

  it('offers the presigned upload form on the configuration versions tab', async () => {
    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });
    await userEvent.click(
      screen.getByRole('tab', { name: 'Configuration versions' })
    );

    expect(
      await screen.findByRole('form', {
        name: 'Upload a configuration version',
      })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Plan and apply' })
    ).toBeInTheDocument();
  });

  it('lists the workspace runs on the runs tab', async () => {
    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });
    await userEvent.click(screen.getByRole('tab', { name: 'Runs' }));

    expect(await screen.findByTestId('run-state-badge')).toHaveAttribute(
      'data-state',
      'planned'
    );
    expect(apiMock.listRuns).toHaveBeenCalledWith(
      { workspace_id: 'ws-01J000000000000000000000' },
      expect.anything()
    );
  });
});
