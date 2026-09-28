import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes, useLocation } from 'react-router-dom';

import { Layout } from '../components/Layout';
import {
  aConfigVersion,
  aRun,
  aRunPlan,
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

const fetchRunPlan = vi.fn();
vi.mock('../api/runPlan', () => ({
  fetchRunPlan: (...args: unknown[]) =>
    (fetchRunPlan as (...a: unknown[]) => unknown)(...args),
}));

const { workspaceRoutes } = await import('./workspaceRoutes');

const WS = 'ws-01J000000000000000000000';

/** Prints the current path, so a test can read where a click landed. */
function Where(): React.ReactElement {
  const location = useLocation();
  return <output data-testid="where">{location.pathname}</output>;
}

/** Mounts the workspace pages inside the shell at the given path. */
function renderAt(path: string): void {
  renderWithAuth(
    <>
      <Routes>
        <Route element={<Layout />}>
          {workspaceRoutes()}
          <Route path="*" element={<p>No such page</p>} />
        </Route>
      </Routes>
      <Where />
    </>,
    signedInAuthClient(),
    [path]
  );
}

describe('workspace navigation', () => {
  beforeEach(() => {
    resetApiMock();
    fetchRunPlan.mockReset();
    fetchRunPlan.mockResolvedValue(aRunPlan());
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.listConfigVersions.mockResolvedValue({
      items: [aConfigVersion()],
    });
    apiMock.listRuns.mockResolvedValue({ items: [aRun('applied')] });
    apiMock.getRun.mockResolvedValue(aRun('applied'));
    apiMock.getRunLogs.mockResolvedValue({
      run_id: 'run-01J000000000000000000000',
      phase: 'plan',
      events: [],
      next_after: null,
    });
  });

  it.each([
    ['the current run card', 0],
    ['the run list', -1],
  ])('opens a run from %s on the runs page', async (_where, index) => {
    renderAt(`/workspaces/${WS}/runs`);

    const links = await screen.findAllByRole('link', {
      name: 'Seed the stack.',
    });
    await userEvent.click(links.at(index)!);

    await waitFor(() => {
      expect(screen.getByTestId('where')).toHaveTextContent(
        `/workspaces/${WS}/runs/run-01J000000000000000000000`
      );
    });
    expect(
      await screen.findByRole('navigation', { name: 'Run breadcrumb' })
    ).toBeInTheDocument();
  });

  it('opens general settings from the settings link on a run page', async () => {
    renderAt(`/workspaces/${WS}/runs/run-01J000000000000000000000`);

    await screen.findByRole('navigation', { name: 'Run breadcrumb' });
    const settings = screen.getAllByRole('link', { name: 'Settings' })[0]!;
    await userEvent.click(settings);

    await waitFor(() => {
      expect(screen.getByTestId('where')).toHaveTextContent(
        `/workspaces/${WS}/settings/general`
      );
    });
    expect(screen.queryByText('No such page')).not.toBeInTheDocument();
  });

  it('sends the bare settings path to general settings', async () => {
    renderAt(`/workspaces/${WS}/settings`);

    await waitFor(() => {
      expect(screen.getByTestId('where')).toHaveTextContent(
        `/workspaces/${WS}/settings/general`
      );
    });
    expect(screen.queryByText('No such page')).not.toBeInTheDocument();
  });
});
