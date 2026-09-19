import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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
      items: [
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
    apiMock.listWorkspaces.mockResolvedValue({ items: [] });

    renderWithAuth(<Workspaces />, signedInAuthClient());

    expect(await screen.findByText('No workspaces yet.')).toBeInTheDocument();
  });

  it('posts the run role ARN with the rest of the create body', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ items: [] });
    apiMock.createWorkspace.mockResolvedValue(aWorkspace());

    renderWithAuth(<Workspaces />, signedInAuthClient());

    await screen.findByRole('form', { name: 'Create a workspace' });
    await userEvent.type(screen.getByLabelText('Name'), 'platform');
    await userEvent.type(
      screen.getByLabelText('Run role ARN'),
      'arn:aws:iam::123456789012:role/terraform-run'
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Create workspace' })
    );

    expect(apiMock.createWorkspace).toHaveBeenCalledWith({
      name: 'platform',
      engine: 'terraform',
      engine_version: '1.11.0',
      run_role_arn: 'arn:aws:iam::123456789012:role/terraform-run',
    });
  });

  it('refuses to post a run role ARN that is not one', async () => {
    apiMock.listWorkspaces.mockResolvedValue({ items: [] });

    renderWithAuth(<Workspaces />, signedInAuthClient());

    await screen.findByRole('form', { name: 'Create a workspace' });
    await userEvent.type(screen.getByLabelText('Name'), 'platform');
    await userEvent.type(screen.getByLabelText('Run role ARN'), 'not-an-arn');
    await userEvent.click(
      screen.getByRole('button', { name: 'Create workspace' })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Enter a role ARN like arn:aws:iam::123456789012:role/terraform-run.'
    );
    expect(apiMock.createWorkspace).not.toHaveBeenCalled();
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
