import { screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { aWorkspace } from '../test-helpers/fixtures';
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

describe('Workspaces', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('renders the workspaces the API returned, each linking to its detail', async () => {
    apiMock.listWorkspaces.mockResolvedValue({
      workspaces: [
        aWorkspace(),
        aWorkspace({ workspace_id: 'ws-2', name: 'organization' }),
      ],
    });

    renderWithAuth(<Workspaces />, signedInAuthClient());

    expect(
      await screen.findByRole('link', { name: 'platform' })
    ).toHaveAttribute('href', '/workspaces/ws-01J000000000000000000000');
    expect(screen.getByRole('link', { name: 'organization' })).toHaveAttribute(
      'href',
      '/workspaces/ws-2'
    );
    expect(
      screen.getByRole('form', { name: 'Create a workspace' })
    ).toBeInTheDocument();
  });

  it('says so when there are no workspaces', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ workspaces: [] });

    renderWithAuth(<Workspaces />, signedInAuthClient());

    expect(await screen.findByText('No workspaces yet.')).toBeInTheDocument();
  });

  it('surfaces a failed read without losing the create form', async () => {
    apiMock.listWorkspaces.mockRejectedValue(
      new Error('Missing the read scope.')
    );

    renderWithAuth(<Workspaces />, signedInAuthClient());

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Missing the read scope.'
    );
    expect(
      screen.getByRole('form', { name: 'Create a workspace' })
    ).toBeInTheDocument();
  });
});
