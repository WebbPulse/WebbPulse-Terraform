import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { AuthClient } from '@webbpulse/auth';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { Layout } from '../../../components/Layout';
import { aWorkspace } from '../../../test-helpers/fixtures';
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
import { anApp, anInstallation, noApp } from '../../settings/fixtures';

vi.mock('../../../api/client', () => apiClientModuleMock());

const { workspaceRoutes } = await import('../../workspaceRoutes');

const WORKSPACE_ID = 'ws-01J000000000000000000000';

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

/** Mounts the version control settings page inside the shell. */
function renderPage(client: AuthClient<unknown> = adminClient()): void {
  renderWithAuth(
    <Routes>
      <Route element={<Layout />}>{workspaceRoutes()}</Route>
    </Routes>,
    client,
    [`/workspaces/${WORKSPACE_ID}/settings/version-control`]
  );
}

/** One repository the App can see. */
function aRepository(fullName: string, defaultBranch = 'main') {
  return {
    id: fullName.length,
    name: fullName.split('/')[1] ?? fullName,
    full_name: fullName,
    private: false,
    html_url: `https://github.com/${fullName}`,
    default_branch: defaultBranch,
  };
}

/** The App installed on one account covering two repositories. */
function withInstalledRepositories(): void {
  apiMock.getGitHubApp.mockResolvedValue(anApp());
  apiMock.listGitHubInstallations.mockResolvedValue({
    items: [anInstallation()],
  });
  apiMock.listGitHubRepositories.mockResolvedValue({
    items: [
      aRepository('WebbPulse/infra', 'staging'),
      aRepository('WebbPulse/WebbPulse-Terraform'),
    ],
  });
}

/** The settings form. */
async function form(): Promise<HTMLElement> {
  return screen.findByRole('form', { name: 'Version control settings' });
}

describe('VersionControlSettings', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.listConfigVersions.mockResolvedValue({ items: [] });
    apiMock.listRuns.mockResolvedValue({ items: [] });
  });

  it('connects a picked repository on its default branch', async () => {
    withInstalledRepositories();
    apiMock.updateWorkspace.mockResolvedValue(
      aWorkspace({ vcs_repo: 'WebbPulse/infra', tracked_branch: 'staging' })
    );
    renderPage();

    await userEvent.click(
      await screen.findByRole('option', { name: /WebbPulse\/infra/ })
    );
    const settings = await form();
    const branch = within(settings).getByLabelText('VCS branch');
    expect(branch).toHaveValue('');
    expect(branch).toHaveAttribute('placeholder', 'staging');
    await userEvent.click(
      within(settings).getByRole('button', { name: 'Connect repository' })
    );

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(WORKSPACE_ID, {
      vcs_repo: 'WebbPulse/infra',
      working_directory: 'terraform',
      file_triggers_enabled: true,
      trigger_patterns: [],
      speculative_plans: true,
    });
  });

  it('saves always trigger, patterns and speculative plans without resending the repository', async () => {
    apiMock.getWorkspace.mockResolvedValue(
      aWorkspace({
        vcs_repo: 'WebbPulse/infra',
        tracked_branch: 'staging',
        trigger_patterns: ['modules/**'],
      })
    );
    apiMock.updateWorkspace.mockResolvedValue(aWorkspace());
    renderPage();

    const settings = await form();
    expect(within(settings).getByLabelText('Trigger patterns')).toHaveValue(
      'modules/**'
    );
    await userEvent.click(
      within(settings).getByLabelText(/Automatic speculative plans/)
    );
    await userEvent.click(
      within(settings).getByLabelText(/Always trigger runs/)
    );
    expect(
      within(settings).queryByLabelText('Trigger patterns')
    ).not.toBeInTheDocument();
    await userEvent.click(
      within(settings).getByRole('button', { name: 'Update VCS settings' })
    );

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(WORKSPACE_ID, {
      working_directory: 'terraform',
      file_triggers_enabled: false,
      trigger_patterns: [],
      speculative_plans: false,
    });
    expect(apiMock.getGitHubApp).not.toHaveBeenCalled();
  });

  it('disconnects after confirming', async () => {
    apiMock.getWorkspace.mockResolvedValue(
      aWorkspace({ vcs_repo: 'WebbPulse/infra', tracked_branch: 'staging' })
    );
    apiMock.updateWorkspace.mockResolvedValue(aWorkspace());
    renderPage();

    const settings = await form();
    await userEvent.click(
      within(settings).getByRole('button', { name: 'Disconnect' })
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Disconnect repository' })
    );

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(WORKSPACE_ID, {
      vcs_repo: null,
      tracked_branch: null,
      trigger_patterns: null,
    });
  });

  it('changes to another repository', async () => {
    withInstalledRepositories();
    apiMock.getWorkspace.mockResolvedValue(
      aWorkspace({ vcs_repo: 'WebbPulse/infra', tracked_branch: 'staging' })
    );
    apiMock.updateWorkspace.mockResolvedValue(aWorkspace());
    renderPage();

    const settings = await form();
    await userEvent.click(
      within(settings).getByRole('button', { name: 'Change repository' })
    );
    await userEvent.click(
      await screen.findByRole('option', { name: /WebbPulse-Terraform/ })
    );
    await userEvent.click(
      within(settings).getByRole('button', { name: 'Update VCS settings' })
    );

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(
      WORKSPACE_ID,
      expect.objectContaining({ vcs_repo: 'WebbPulse/WebbPulse-Terraform' })
    );
    expect(apiMock.updateWorkspace.mock.calls[0]?.[1]).not.toHaveProperty(
      'tracked_branch'
    );
  });

  it('shows the connected repository and branch apart, with the branch as a placeholder', async () => {
    apiMock.getWorkspace.mockResolvedValue(
      aWorkspace({ vcs_repo: 'WebbPulse/infra', tracked_branch: 'staging' })
    );
    renderPage();

    const settings = await form();
    const connected = within(settings).getByTestId('connected-repository');
    expect(
      within(connected).getByRole('link', { name: 'WebbPulse/infra' })
    ).toBeInTheDocument();
    expect(within(connected).getByText('staging')).toBeInTheDocument();
    expect(connected).not.toHaveTextContent('WebbPulse/infrastaging');
    const branch = within(settings).getByLabelText('VCS branch');
    expect(branch).toHaveValue('');
    expect(branch).toHaveAttribute('placeholder', 'staging');
  });

  it('sends an admin to set up the App when there is none', async () => {
    apiMock.getGitHubApp.mockResolvedValue(noApp());
    apiMock.listGitHubInstallations.mockResolvedValue({ items: [] });
    renderPage();

    const link = await screen.findByRole('link', {
      name: 'Set up the GitHub App',
    });
    expect(link).toHaveAttribute('href', '/settings/github');
  });

  it('sends an admin to install the App when it has no installation', async () => {
    apiMock.getGitHubApp.mockResolvedValue(anApp());
    apiMock.listGitHubInstallations.mockResolvedValue({ items: [] });
    renderPage();

    expect(
      await screen.findByRole('link', { name: 'Install the GitHub App' })
    ).toHaveAttribute('href', '/settings/github');
  });

  it('filters the repositories', async () => {
    withInstalledRepositories();
    renderPage();

    await screen.findByRole('option', { name: /WebbPulse\/infra/ });
    await userEvent.type(
      screen.getByLabelText('Filter repositories'),
      'terraform'
    );
    expect(
      screen.queryByRole('option', { name: /WebbPulse\/infra/ })
    ).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('Filter repositories'), 'zzz');
    expect(screen.getByText('No repository matches.')).toBeInTheDocument();
  });

  it('tells a non admin who can connect a repository', async () => {
    renderPage(signedInAuthClient());

    expect(
      await screen.findByText(/Only an admin can connect a repository/)
    ).toBeInTheDocument();
    expect(apiMock.getGitHubApp).not.toHaveBeenCalled();
  });
});
