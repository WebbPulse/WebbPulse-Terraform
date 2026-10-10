import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { aFreshWorkspace, aListedWorkspace } from '../test-helpers/fixtures';
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

const { Projects } = await import('./Projects');

const CMP = 'prj-01J000000000000000000001';
const PORTFOLIO = 'prj-01J000000000000000000003';

/** Mounts the projects page. */
function renderProjects(entry = '/projects'): void {
  renderWithAuth(
    <Routes>
      <Route path="/projects" element={<Projects />} />
    </Routes>,
    signedInAuthClient(),
    [entry]
  );
}

describe('Projects', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.listProjects.mockResolvedValue({
      items: [
        {
          project_id: 'prj-default',
          name: 'Default Project',
          is_default: true,
          workspace_count: 0,
        },
        {
          project_id: CMP,
          name: 'CarModPicker',
          description: 'The car site.',
          is_default: false,
          workspace_count: 2,
        },
        {
          project_id: PORTFOLIO,
          name: 'Portfolio',
          is_default: false,
          workspace_count: 0,
        },
      ],
    });
    apiMock.listWorkspaces.mockResolvedValue({
      items: [
        aListedWorkspace(
          aFreshWorkspace({
            workspace_id: 'ws-1',
            name: 'cmp-prod',
            project_id: CMP,
          }),
          {
            latest_run: {
              run_id: 'run-01J000000000000000000001',
              status: 'errored',
              created_at: '2026-10-01T00:00:00Z',
              updated_at: '2026-10-01T00:01:00Z',
              changed_at: '2026-10-01T00:01:00Z',
            },
          }
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
  });

  it('shows each project with its workspaces side by side and their latest runs', async () => {
    renderProjects();

    const row = (
      await screen.findByRole('link', { name: 'CarModPicker' })
    ).closest('tr');
    expect(row).not.toBeNull();
    const cells = within(row as HTMLElement);
    expect(cells.getByRole('link', { name: 'cmp-prod' })).toHaveAttribute(
      'href',
      '/workspaces/ws-1'
    );
    expect(
      cells.getByRole('link', { name: 'cmp-staging' })
    ).toBeInTheDocument();
    expect(
      cells.getByRole('link', { name: 'Latest run: Errored' })
    ).toBeInTheDocument();
    expect(cells.getByText('2 workspaces')).toBeInTheDocument();
    expect(screen.getByText('No workspaces yet')).toBeInTheDocument();
  });

  it('leaves out the default project while it has no workspaces', async () => {
    renderProjects();

    expect(
      await screen.findByRole('link', { name: 'CarModPicker' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: 'Default Project' })
    ).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Portfolio' })).toBeInTheDocument();
    expect(screen.getByText('2 of 2')).toBeInTheDocument();
  });

  it('lists the default project again once it holds a workspace', async () => {
    apiMock.listProjects.mockResolvedValue({
      items: [
        {
          project_id: 'prj-default',
          name: 'Default Project',
          is_default: true,
          workspace_count: 1,
        },
      ],
    });
    apiMock.listWorkspaces.mockResolvedValue({
      items: [aListedWorkspace()],
    });
    renderProjects();

    expect(
      await screen.findByRole('link', { name: 'Default Project' })
    ).toHaveAttribute('href', '/projects/prj-default');
    expect(screen.getByText('1 workspace')).toBeInTheDocument();
  });

  it('says there are no projects yet when only the empty default exists', async () => {
    apiMock.listProjects.mockResolvedValue({
      items: [
        {
          project_id: 'prj-default',
          name: 'Default Project',
          is_default: true,
          workspace_count: 0,
        },
      ],
    });
    apiMock.listWorkspaces.mockResolvedValue({ items: [] });
    renderProjects();

    expect(await screen.findByText('No projects yet.')).toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: 'Default Project' })
    ).not.toBeInTheDocument();
  });

  it('filters the projects by the search the address holds', async () => {
    renderProjects('/projects?q=car');

    expect(
      await screen.findByRole('link', { name: 'CarModPicker' })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: 'Default Project' })
    ).not.toBeInTheDocument();
  });

  it('creates a project from the inline form', async () => {
    apiMock.createProject.mockResolvedValue({
      project_id: 'prj-01J000000000000000000002',
      name: 'Portfolio',
    });
    renderProjects();

    await userEvent.click(
      await screen.findByRole('button', { name: 'New project' })
    );
    await userEvent.type(screen.getByLabelText('Project name'), 'Portfolio');
    await userEvent.click(
      screen.getByRole('button', { name: 'Create project' })
    );

    expect(apiMock.createProject).toHaveBeenCalledWith({ name: 'Portfolio' });
    expect(
      screen.queryByRole('form', { name: 'Create a project' })
    ).not.toBeInTheDocument();
  });
});
