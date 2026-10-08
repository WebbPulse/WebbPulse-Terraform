import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { AuthClient } from '@webbpulse/auth';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { Layout } from '../../../components/Layout';
import { aListedWorkspace, aWorkspace } from '../../../test-helpers/fixtures';
import {
  jsonResponse,
  renderWithAuth,
  signedInAuthClient,
  stubAuthClient,
} from '../../../test-helpers/renderWithAuth';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../../../test-helpers/apiMock';

vi.mock('../../../api/client', () => apiClientModuleMock());

const { workspaceRoutes } = await import('../../workspaceRoutes');

const WORKSPACE_ID = 'ws-01J000000000000000000000';
const TOGGLE = 'Auto-apply API, CLI and VCS runs';

/** An auth client whose user is an admin. */
function adminClient(): AuthClient<unknown> {
  return stubAuthClient({
    fetch: () =>
      Promise.resolve(
        jsonResponse({ access_token: 'test-token', expires_in: 3600 })
      ),
    user: { email: 'admin@webbpulse.com', is_admin: true },
  });
}

/** Mounts the general settings page inside the shell. */
function renderPage(client: AuthClient<unknown> = adminClient()): void {
  renderWithAuth(
    <Routes>
      <Route element={<Layout />}>{workspaceRoutes()}</Route>
    </Routes>,
    client,
    [`/workspaces/${WORKSPACE_ID}/settings/general`]
  );
}

describe('GeneralSettings auto-apply', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.listConfigVersions.mockResolvedValue({ items: [] });
    apiMock.listRuns.mockResolvedValue({ items: [] });
    apiMock.updateWorkspace.mockResolvedValue(aWorkspace({ auto_apply: true }));
  });

  it('lets an admin turn it on', async () => {
    renderPage();

    const toggle = await screen.findByRole('checkbox', { name: TOGGLE });
    expect(toggle).not.toBeChecked();
    await userEvent.click(toggle);
    await userEvent.click(
      screen.getByRole('button', { name: 'Save settings' })
    );

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(
      WORKSPACE_ID,
      expect.objectContaining({ auto_apply: true })
    );
  });

  it('leaves it out of a save that did not change it', async () => {
    renderPage();

    await screen.findByRole('checkbox', { name: TOGGLE });
    await userEvent.click(
      screen.getByRole('button', { name: 'Save settings' })
    );

    expect(apiMock.updateWorkspace).toHaveBeenCalledTimes(1);
    expect(apiMock.updateWorkspace.mock.calls[0]?.[1]).not.toHaveProperty(
      'auto_apply'
    );
  });

  it('is read only for someone who is not an admin', async () => {
    apiMock.getWorkspace.mockResolvedValue(aWorkspace({ auto_apply: true }));
    renderPage(signedInAuthClient());

    const toggle = await screen.findByRole('checkbox', { name: TOGGLE });
    expect(toggle).toBeChecked();
    expect(toggle).toBeDisabled();
    expect(
      screen.getByText(/Only an admin can change this\./)
    ).toBeInTheDocument();
  });
});

describe('GeneralSettings project', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.listConfigVersions.mockResolvedValue({ items: [] });
    apiMock.listRuns.mockResolvedValue({ items: [] });
    apiMock.listProjects.mockResolvedValue({
      items: [
        { project_id: 'prj-default', name: 'Default Project' },
        { project_id: 'prj-01J000000000000000000001', name: 'Platform' },
      ],
    });
    apiMock.updateWorkspace.mockResolvedValue(
      aWorkspace({ project_id: 'prj-01J000000000000000000001' })
    );
  });

  it('moves the workspace to another project and nothing else', async () => {
    renderPage();

    const picker = await screen.findByRole('combobox', { name: 'Project' });
    expect(picker).toHaveValue('prj-default');
    await userEvent.selectOptions(picker, 'prj-01J000000000000000000001');
    await userEvent.click(
      screen.getByRole('button', { name: 'Move workspace' })
    );

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(WORKSPACE_ID, {
      project_id: 'prj-01J000000000000000000001',
    });
  });
});

describe('GeneralSettings remote state sharing', () => {
  const NETWORK_ID = 'ws-01J000000000000000000001';
  const DNS_ID = 'ws-01J000000000000000000002';

  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.listConfigVersions.mockResolvedValue({ items: [] });
    apiMock.listRuns.mockResolvedValue({ items: [] });
    apiMock.listWorkspaces.mockResolvedValue({
      items: [
        aListedWorkspace(aWorkspace()),
        aListedWorkspace(aWorkspace({ workspace_id: DNS_ID, name: 'dns' })),
        aListedWorkspace(
          aWorkspace({ workspace_id: NETWORK_ID, name: 'network' })
        ),
      ],
    });
    apiMock.updateWorkspace.mockResolvedValue(aWorkspace());
  });

  it('shares with specific workspaces', async () => {
    renderPage();

    const form = await screen.findByRole('form', {
      name: 'Remote state sharing',
    });
    const save = within(form).getByRole('button', {
      name: 'Save remote state sharing',
    });
    expect(save).toBeDisabled();
    expect(
      within(form).queryByRole('checkbox', { name: 'platform' })
    ).not.toBeInTheDocument();
    await userEvent.click(
      await within(form).findByRole('checkbox', { name: 'network' })
    );
    await userEvent.click(save);

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(WORKSPACE_ID, {
      global_remote_state: false,
      remote_state_consumer_ids: [NETWORK_ID],
    });
  });

  it('shares with every workspace and hides the picker', async () => {
    apiMock.getWorkspace.mockResolvedValue(
      aWorkspace({ remote_state_consumer_ids: [DNS_ID] })
    );
    renderPage();

    const form = await screen.findByRole('form', {
      name: 'Remote state sharing',
    });
    await userEvent.click(
      within(form).getByRole('checkbox', {
        name: 'Share with all workspaces in this organization',
      })
    );
    expect(
      within(form).queryByRole('checkbox', { name: 'dns' })
    ).not.toBeInTheDocument();
    await userEvent.click(
      within(form).getByRole('button', { name: 'Save remote state sharing' })
    );

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(WORKSPACE_ID, {
      global_remote_state: true,
      remote_state_consumer_ids: [DNS_ID],
    });
  });
});
