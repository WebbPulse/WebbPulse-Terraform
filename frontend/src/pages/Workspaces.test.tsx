import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import {
  aFreshWorkspace,
  aListedWorkspace,
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
import type { LatestRun } from '../api';
import { formatDateTime } from '../components';

vi.mock('../api/client', () => apiClientModuleMock());

const { Workspaces } = await import('./Workspaces');

/** Mounts the list with a detail route beside it, so navigation can be seen. */
function renderList(entry = '/workspaces'): void {
  renderWithAuth(
    <Routes>
      <Route path="/workspaces" element={<Workspaces />} />
      <Route path="/workspaces/:workspaceId" element={<p>Detail page</p>} />
    </Routes>,
    signedInAuthClient(),
    [entry]
  );
}

describe('Workspaces', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('shows each row the same check the workspace header reads, without writing', async () => {
    apiMock.listWorkspaces.mockResolvedValue({
      items: [
        aListedWorkspace(
          aWorkspace({ run_role_account_id: null, run_role_checked_at: null })
        ),
        aListedWorkspace(
          aFreshWorkspace({ workspace_id: 'ws-2', name: 'organization' })
        ),
      ],
    });
    apiMock.readRunRoleCheck.mockResolvedValue({
      connected: true,
      status: 'connected',
      account_id: '123456789012',
      error: null,
      run_id: 'run-1',
      checked_at: '2026-09-17T00:05:00Z',
    });

    renderList();

    expect(await screen.findByText('123456789012')).toBeInTheDocument();
    expect(screen.getByText('Not connected')).toBeInTheDocument();
    expect(apiMock.readRunRoleCheck).toHaveBeenCalledTimes(1);
    expect(apiMock.readRunRoleCheck).toHaveBeenCalledWith(
      'ws-01J000000000000000000000',
      expect.objectContaining({ signal: expect.any(AbortSignal) as unknown })
    );
    expect(apiMock.checkRunRole).not.toHaveBeenCalled();
  });

  it('renders the workspaces the API returned, each linking to its detail', async () => {
    apiMock.listWorkspaces.mockResolvedValue({
      items: [
        aListedWorkspace(),
        aListedWorkspace(
          aFreshWorkspace({ workspace_id: 'ws-2', name: 'organization' })
        ),
        aListedWorkspace(
          aFreshWorkspace({
            workspace_id: 'ws-3',
            name: 'network',
            run_role_arn: aWorkspace().run_role_arn,
          })
        ),
      ],
    });

    renderList();

    expect(
      await screen.findByRole('link', { name: 'platform' })
    ).toHaveAttribute('href', '/workspaces/ws-01J000000000000000000000');
    expect(screen.getByRole('link', { name: 'organization' })).toHaveAttribute(
      'href',
      '/workspaces/ws-2'
    );
    expect(screen.getByText('123456789012')).toBeInTheDocument();
    expect(screen.getByText('Not connected')).toBeInTheDocument();
    expect(screen.getByText('Not verified')).toBeInTheDocument();
  });

  it('shows the newest run as the latest change, with its status linking to the run', async () => {
    const settled = aWorkspace({ updated_at: '2026-09-17T01:00:00Z' });
    const latestRun: LatestRun = {
      run_id: 'run-01J000000000000000000009',
      status: 'awaiting_confirmation',
      created_at: '2026-09-18T00:00:00Z',
      updated_at: '2026-09-18T00:02:00Z',
      changed_at: '2026-09-18T00:02:00Z',
    };
    apiMock.listWorkspaces.mockResolvedValue({
      items: [
        aListedWorkspace(settled, {
          latest_run: latestRun,
          latest_change_at: latestRun.changed_at,
        }),
      ],
    });

    renderList();

    const status = await screen.findByRole('link', {
      name: 'Latest run: Needs confirmation',
    });
    expect(status).toHaveAttribute(
      'href',
      '/workspaces/ws-01J000000000000000000000/runs/run-01J000000000000000000009'
    );
    expect(
      screen.getByTitle(formatDateTime('2026-09-18T00:02:00Z'))
    ).toHaveAttribute('datetime', '2026-09-18T00:02:00Z');
    expect(
      screen.queryByTitle(formatDateTime('2026-09-17T01:00:00Z'))
    ).not.toBeInTheDocument();
  });

  it('says a workspace that never ran has no runs and falls back to its own change', async () => {
    apiMock.listWorkspaces.mockResolvedValue({
      items: [
        aListedWorkspace(aWorkspace({ updated_at: '2026-09-17T01:00:00Z' })),
      ],
    });

    renderList();

    expect(await screen.findByText('No runs yet')).toBeInTheDocument();
    expect(
      screen.getByTitle(formatDateTime('2026-09-17T01:00:00Z'))
    ).toHaveAttribute('datetime', '2026-09-17T01:00:00Z');
  });

  it('says so once when there are no workspaces', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ items: [] });

    renderList();

    expect(await screen.findByText('No workspaces yet.')).toBeInTheDocument();
    expect(
      screen.getAllByText(
        'Connect a repository, or upload configuration from the CLI or the API.'
      )
    ).toHaveLength(1);
    expect(
      screen.getByRole('link', { name: 'Create the first workspace' })
    ).toHaveAttribute('href', '/workspaces/new');
  });

  it('links New workspace to the create page rather than opening a dialog', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ items: [] });

    renderList();

    await screen.findByText('No workspaces yet.');
    expect(screen.getByRole('link', { name: 'New workspace' })).toHaveAttribute(
      'href',
      '/workspaces/new'
    );
    expect(
      screen.queryByRole('button', { name: 'New workspace' })
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('surfaces a failed read without losing the create action', async () => {
    apiMock.listWorkspaces.mockRejectedValue(
      new Error('Missing the read scope.')
    );

    renderList();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Missing the read scope.'
    );
    expect(screen.getByRole('link', { name: 'New workspace' })).toHaveAttribute(
      'href',
      '/workspaces/new'
    );
  });

  it('asks the API for the sort the address holds and starts on name', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ items: [aListedWorkspace()] });

    renderList('/workspaces?sort=-latest_run');

    expect(await screen.findByLabelText('Sort workspaces')).toHaveValue(
      '-latest_run'
    );
    expect(apiMock.listWorkspaces).toHaveBeenCalledWith(expect.anything(), {
      sort: '-latest_run',
    });
  });

  it('changes the sort through the control and asks the API again', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ items: [aListedWorkspace()] });
    renderList();

    const select = await screen.findByLabelText('Sort workspaces');
    expect(select).toHaveValue('name');
    await userEvent.selectOptions(select, 'status');

    expect(apiMock.listWorkspaces).toHaveBeenLastCalledWith(expect.anything(), {
      sort: 'status',
    });
  });

  it('filters by the search the address holds', async () => {
    apiMock.listWorkspaces.mockResolvedValue({
      items: [
        aListedWorkspace(),
        aListedWorkspace(
          aFreshWorkspace({ workspace_id: 'ws-2', name: 'organization' })
        ),
      ],
    });

    renderList('/workspaces?q=org');

    expect(
      await screen.findByRole('link', { name: 'organization' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: 'platform' })
    ).not.toBeInTheDocument();
    expect(screen.getByText('1 of 2', { exact: false })).toBeInTheDocument();
  });

  it('groups the workspaces by project, the default first, keeping the order inside each', async () => {
    apiMock.listWorkspaces.mockResolvedValue({
      items: [
        aListedWorkspace(
          aFreshWorkspace({
            workspace_id: 'ws-2',
            name: 'cmp-prod',
            project_id: 'prj-01J000000000000000000001',
          })
        ),
        aListedWorkspace(),
        aListedWorkspace(
          aFreshWorkspace({
            workspace_id: 'ws-3',
            name: 'cmp-staging',
            project_id: 'prj-01J000000000000000000001',
          })
        ),
      ],
    });
    apiMock.listProjects.mockResolvedValue({
      items: [
        {
          project_id: 'prj-default',
          name: 'Default Project',
          is_default: true,
          workspace_count: 1,
        },
        {
          project_id: 'prj-01J000000000000000000001',
          name: 'CarModPicker',
          is_default: false,
          workspace_count: 2,
        },
      ],
    });

    renderList('/workspaces?group=project');

    const carModPicker = await screen.findByRole('region', {
      name: 'Project CarModPicker',
    });
    const names = within(carModPicker)
      .getAllByRole('link')
      .map((link) => link.textContent);
    expect(names).toEqual(['CarModPicker', 'cmp-prod', 'cmp-staging']);
    expect(
      within(
        screen.getByRole('region', { name: 'Project Default Project' })
      ).getByRole('link', { name: 'platform' })
    ).toBeInTheDocument();
    expect(
      within(carModPicker).getByRole('link', { name: 'CarModPicker' })
    ).toHaveAttribute('href', '/projects/prj-01J000000000000000000001');
  });
});
