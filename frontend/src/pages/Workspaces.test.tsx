import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { aFreshWorkspace, aWorkspace } from '../test-helpers/fixtures';
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

const { Workspaces } = await import('./Workspaces');

/** Mounts the list with a detail route beside it, so navigation can be seen. */
function renderList(): void {
  renderWithAuth(
    <Routes>
      <Route path="/workspaces" element={<Workspaces />} />
      <Route path="/workspaces/:workspaceId" element={<p>Detail page</p>} />
    </Routes>,
    signedInAuthClient(),
    ['/workspaces']
  );
}

/** Opens the create dialog and returns its form. */
async function openCreateForm(): Promise<HTMLElement> {
  await userEvent.click(screen.getByRole('button', { name: 'New workspace' }));
  return screen.getByRole('form', { name: 'Create a workspace' });
}

describe('Workspaces', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('renders the workspaces the API returned, each linking to its detail', async () => {
    apiMock.listWorkspaces.mockResolvedValue({
      items: [
        aWorkspace(),
        aFreshWorkspace({ workspace_id: 'ws-2', name: 'organization' }),
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
  });

  it('says so when there are no workspaces', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ items: [] });

    renderList();

    expect(await screen.findByText('No workspaces yet.')).toBeInTheDocument();
  });

  it('keeps the create form behind the New workspace button', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ items: [] });

    renderList();

    await screen.findByText('No workspaces yet.');
    expect(
      screen.queryByRole('form', { name: 'Create a workspace' })
    ).not.toBeInTheDocument();

    const form = await openCreateForm();

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(within(form).getByLabelText('Name')).toHaveFocus();
    expect(within(form).queryByLabelText(/role/i)).not.toBeInTheDocument();
  });

  it('creates a workspace from its name alone and opens it', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ items: [] });
    apiMock.createWorkspace.mockResolvedValue(aFreshWorkspace());

    renderList();

    await screen.findByText('No workspaces yet.');
    const form = await openCreateForm();
    await userEvent.type(within(form).getByLabelText('Name'), 'platform');
    await userEvent.click(
      within(form).getByRole('button', { name: 'Create workspace' })
    );

    expect(apiMock.createWorkspace).toHaveBeenCalledWith({
      name: 'platform',
      engine: 'terraform',
      engine_version: '1.11.0',
    });
    expect(await screen.findByText('Detail page')).toBeInTheDocument();
  });

  it('sends a description when one is given', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ items: [] });
    apiMock.createWorkspace.mockResolvedValue(aFreshWorkspace());

    renderList();

    await screen.findByText('No workspaces yet.');
    const form = await openCreateForm();
    await userEvent.type(within(form).getByLabelText('Name'), 'platform');
    await userEvent.type(
      within(form).getByLabelText('Description'),
      'The platform workspace.'
    );
    await userEvent.click(
      within(form).getByRole('button', { name: 'Create workspace' })
    );

    expect(apiMock.createWorkspace).toHaveBeenCalledWith({
      name: 'platform',
      engine: 'terraform',
      engine_version: '1.11.0',
      description: 'The platform workspace.',
    });
  });

  it('closes the dialog on Escape without creating anything', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ items: [] });

    renderList();

    await screen.findByText('No workspaces yet.');
    await openCreateForm();
    await userEvent.keyboard('{Escape}');

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(apiMock.createWorkspace).not.toHaveBeenCalled();
  });

  it('surfaces a failed read without losing the create action', async () => {
    apiMock.listWorkspaces.mockRejectedValue(
      new Error('Missing the read scope.')
    );

    renderList();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Missing the read scope.'
    );
    expect(
      screen.getByRole('button', { name: 'New workspace' })
    ).toBeInTheDocument();
  });
});
