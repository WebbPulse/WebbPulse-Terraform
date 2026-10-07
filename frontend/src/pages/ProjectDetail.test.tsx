import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import {
  aFreshWorkspace,
  aListedWorkspace,
  aRun,
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

const { ProjectDetail } = await import('./ProjectDetail');

const CMP = 'prj-01J000000000000000000001';

/** Mounts the project page with the projects list beside it. */
function renderProject(entry = `/projects/${CMP}`): void {
  renderWithAuth(
    <Routes>
      <Route path="/projects/:projectId" element={<ProjectDetail />} />
      <Route path="/projects" element={<p>Projects list</p>} />
    </Routes>,
    signedInAuthClient(),
    [entry]
  );
}

describe('ProjectDetail', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getProject.mockResolvedValue({
      project_id: CMP,
      name: 'CarModPicker',
      description: 'The car site.',
      is_default: false,
      workspace_count: 0,
    });
    apiMock.listWorkspaces.mockResolvedValue({
      items: [
        aListedWorkspace(
          aFreshWorkspace({
            workspace_id: 'ws-01J000000000000000000000',
            name: 'cmp-prod',
            project_id: CMP,
          })
        ),
        aListedWorkspace(
          aFreshWorkspace({
            workspace_id: 'ws-2',
            name: 'cmp-staging',
            project_id: CMP,
          })
        ),
      ],
    });
    apiMock.listRuns.mockResolvedValue({
      items: [aRun('applied', { message: 'Ship the car site.' })],
      next_cursor: null,
    });
  });

  it('lists the project workspaces in the sort the address holds, and its recent runs', async () => {
    renderProject(`/projects/${CMP}?sort=-name`);

    expect(
      await screen.findByRole('heading', { name: 'CarModPicker' })
    ).toBeInTheDocument();
    expect(apiMock.listWorkspaces).toHaveBeenCalledWith(expect.anything(), {
      project_id: CMP,
      sort: '-name',
    });
    expect(apiMock.listRuns).toHaveBeenCalledWith(
      { project_id: CMP, limit: 20 },
      expect.anything()
    );
    const recent = screen.getByRole('region', { name: 'Recent runs' });
    expect(
      await within(recent).findByText('Ship the car site.', { exact: false })
    ).toBeInTheDocument();
  });

  it('filters the project workspaces by name', async () => {
    renderProject();

    await screen.findByLabelText('Filter workspaces by name');
    const list = screen.getByRole('region', { name: 'Workspaces' });
    await userEvent.type(
      screen.getByLabelText('Filter workspaces by name'),
      'stag'
    );

    expect(
      within(list).queryByRole('link', { name: 'cmp-prod' })
    ).not.toBeInTheDocument();
    expect(
      within(list).getByRole('link', { name: 'cmp-staging' })
    ).toBeInTheDocument();
  });

  it('links New workspace to the create page with this project chosen', async () => {
    renderProject();

    expect(
      await screen.findByRole('link', { name: 'New workspace' })
    ).toHaveAttribute('href', `/workspaces/new?project=${CMP}`);
  });

  it('deletes an empty project after a confirmation and returns to the list', async () => {
    apiMock.deleteProject.mockResolvedValue(undefined);
    renderProject();

    await userEvent.click(
      await screen.findByRole('button', { name: 'Delete project' })
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Delete this project' })
    );

    expect(apiMock.deleteProject).toHaveBeenCalledWith(CMP);
    expect(await screen.findByText('Projects list')).toBeInTheDocument();
  });

  it('offers no settings on the default project', async () => {
    apiMock.getProject.mockResolvedValue({
      project_id: 'prj-default',
      name: 'Default Project',
      is_default: true,
      workspace_count: 2,
    });
    renderProject('/projects/prj-default');

    await screen.findByRole('heading', { name: 'Default Project' });
    expect(
      screen.queryByRole('button', { name: 'Delete project' })
    ).not.toBeInTheDocument();
  });
});
