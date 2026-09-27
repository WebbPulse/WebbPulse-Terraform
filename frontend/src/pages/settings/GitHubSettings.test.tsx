import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import {
  renderWithAuth,
  signedInAuthClient,
} from '../../test-helpers/renderWithAuth';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../../test-helpers/apiMock';
import { anApp, anInstallation, noApp } from './fixtures';

vi.mock('../../api/client', () => apiClientModuleMock());
vi.mock('./githubNavigation', () => ({ postManifest: vi.fn(), goTo: vi.fn() }));

const navigation = await import('./githubNavigation');
const { GitHubSettings } = await import('./GitHubSettings');

/** Mounts the page at its route. */
function renderPage(): void {
  renderWithAuth(
    <Routes>
      <Route path="/settings/github" element={<GitHubSettings />} />
    </Routes>,
    signedInAuthClient(),
    ['/settings/github']
  );
}

describe('GitHubSettings', () => {
  beforeEach(() => {
    resetApiMock();
    vi.mocked(navigation.postManifest).mockReset();
    vi.mocked(navigation.goTo).mockReset();
  });

  it('offers creation when there is no App, and posts the manifest to GitHub', async () => {
    apiMock.getGitHubApp.mockResolvedValue(noApp());
    const manifest = { name: 'WebbPulse Terraform (staging)' };
    apiMock.startGitHubManifest.mockResolvedValue({
      action_url:
        'https://github.com/organizations/WebbPulse/settings/apps/new?state=s1',
      manifest,
      state: 's1',
    });

    renderPage();

    const form = await screen.findByRole('form', {
      name: 'Create a GitHub App',
    });
    expect(
      within(form).getByRole('radio', { name: /^Organization/ })
    ).toBeChecked();
    expect(within(form).getByLabelText('Organization name')).toHaveValue(
      'WebbPulse'
    );
    await userEvent.click(
      within(form).getByRole('button', { name: 'Create GitHub App' })
    );

    expect(apiMock.startGitHubManifest).toHaveBeenCalledWith({
      organization: 'WebbPulse',
    });
    expect(navigation.postManifest).toHaveBeenCalledWith(
      'https://github.com/organizations/WebbPulse/settings/apps/new?state=s1',
      manifest
    );
    expect(apiMock.listGitHubInstallations).not.toHaveBeenCalled();
  });

  it('creates on the personal account only when that is chosen', async () => {
    apiMock.getGitHubApp.mockResolvedValue(noApp());
    apiMock.startGitHubManifest.mockResolvedValue({
      action_url: 'https://github.com/settings/apps/new?state=s1',
      manifest: {},
      state: 's1',
    });

    renderPage();

    await userEvent.click(
      await screen.findByRole('radio', { name: /^Personal account/ })
    );
    expect(
      screen.queryByLabelText('Organization name')
    ).not.toBeInTheDocument();
    await userEvent.click(
      screen.getByRole('button', { name: 'Create GitHub App' })
    );
    expect(apiMock.startGitHubManifest).toHaveBeenCalledWith({});
  });

  it('requires an organization name, and creates under the one typed', async () => {
    apiMock.getGitHubApp.mockResolvedValue(noApp());
    apiMock.startGitHubManifest.mockResolvedValue({
      action_url:
        'https://github.com/organizations/Acme/settings/apps/new?state=s1',
      manifest: {},
      state: 's1',
    });

    renderPage();

    const input = await screen.findByLabelText('Organization name');
    expect(input).toBeRequired();
    await userEvent.clear(input);
    await userEvent.click(
      screen.getByRole('button', { name: 'Create GitHub App' })
    );
    expect(apiMock.startGitHubManifest).not.toHaveBeenCalled();
    await userEvent.type(input, 'Acme');
    await userEvent.click(
      screen.getByRole('button', { name: 'Create GitHub App' })
    );
    expect(apiMock.startGitHubManifest).toHaveBeenCalledWith({
      organization: 'Acme',
    });
  });

  it('shows skeletons while the App status loads', async () => {
    apiMock.getGitHubApp.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(
      await screen.findByRole('status', { name: 'Loading GitHub settings' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('navigation', { name: 'Breadcrumb' })
    ).toHaveTextContent(/^Settings$/);
    expect(
      screen.getByRole('heading', { level: 1, name: 'GitHub' })
    ).toBeInTheDocument();
  });

  it('shows the refusal a non admin gets', async () => {
    apiMock.getGitHubApp.mockRejectedValue(
      new Error('You do not have access to GitHub settings.')
    );

    renderPage();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'You do not have access'
    );
    expect(
      screen.queryByRole('button', { name: 'Create GitHub App' })
    ).not.toBeInTheDocument();
  });

  it('lists installations with their actions once the App exists', async () => {
    apiMock.getGitHubApp.mockResolvedValue(anApp());
    apiMock.listGitHubInstallations.mockResolvedValue({
      items: [anInstallation()],
    });

    renderPage();

    const table = await screen.findByRole('table', { name: 'Installations' });
    expect(within(table).getByText('WebbPulse')).toBeInTheDocument();
    expect(
      within(table).getByText('Selected repositories')
    ).toBeInTheDocument();
    expect(
      within(table).getByRole('link', { name: 'Configure WebbPulse on GitHub' })
    ).toHaveAttribute(
      'href',
      'https://github.com/organizations/WebbPulse/settings/installations/77'
    );
    expect(
      screen.queryByRole('button', { name: 'Create GitHub App' })
    ).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'App settings' })).toHaveAttribute(
      'href',
      anApp().settings_url
    );
  });

  it('sends the browser to the install URL', async () => {
    apiMock.getGitHubApp.mockResolvedValue(anApp());
    apiMock.listGitHubInstallations.mockResolvedValue({ items: [] });
    apiMock.startGitHubInstall.mockResolvedValue({
      install_url:
        'https://github.com/apps/webbpulse-terraform-staging/installations/new?state=s2',
      state: 's2',
    });

    renderPage();

    expect(
      await screen.findByText('Not installed anywhere yet.')
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole('button', { name: 'Install on repositories' })
    );
    expect(navigation.goTo).toHaveBeenCalledWith(
      'https://github.com/apps/webbpulse-terraform-staging/installations/new?state=s2'
    );
  });

  it('disables install when the App has no slug', async () => {
    apiMock.getGitHubApp.mockResolvedValue(
      anApp({ can_install: false, slug: null })
    );
    apiMock.listGitHubInstallations.mockResolvedValue({ items: [] });

    renderPage();

    expect(
      await screen.findByRole('button', { name: 'Install on repositories' })
    ).toBeDisabled();
  });

  it('refreshes and removes an installation', async () => {
    apiMock.getGitHubApp.mockResolvedValue(anApp());
    apiMock.listGitHubInstallations.mockResolvedValue({
      items: [anInstallation()],
    });
    apiMock.refreshGitHubInstallation.mockResolvedValue(anInstallation());
    apiMock.removeGitHubInstallation.mockResolvedValue(undefined);

    renderPage();

    await userEvent.click(
      await screen.findByRole('button', { name: 'Refresh WebbPulse' })
    );
    expect(apiMock.refreshGitHubInstallation).toHaveBeenCalledWith('77');

    await userEvent.click(
      screen.getByRole('button', { name: 'Remove WebbPulse' })
    );
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Remove installation' })
    );
    expect(apiMock.removeGitHubInstallation).toHaveBeenCalledWith('77');
  });

  it('counts selected repositories when GitHub reported them', async () => {
    apiMock.getGitHubApp.mockResolvedValue(anApp());
    apiMock.listGitHubInstallations.mockResolvedValue({
      items: [
        anInstallation({ repository_count: 1 }),
        anInstallation({
          installation_id: '78',
          account_login: 'tyler',
          repository_selection: 'all',
        }),
      ],
    });

    renderPage();

    const table = await screen.findByRole('table', { name: 'Installations' });
    expect(within(table).getByText('1 selected')).toBeInTheDocument();
    expect(within(table).getByText('All repositories')).toBeInTheDocument();
  });

  it('opens the logo steps from a disclosure', async () => {
    apiMock.getGitHubApp.mockResolvedValue(anApp());
    apiMock.listGitHubInstallations.mockResolvedValue({ items: [] });

    renderPage();

    const toggle = await screen.findByRole('button', {
      name: 'Logo and badge colour',
    });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Upload the logo')).not.toBeInTheDocument();
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Upload the logo')).toBeInTheDocument();
  });

  it("loads an installation's repositories only when opened", async () => {
    apiMock.getGitHubApp.mockResolvedValue(anApp());
    apiMock.listGitHubInstallations.mockResolvedValue({
      items: [anInstallation({ repository_count: 1 })],
    });
    apiMock.listGitHubRepositories.mockResolvedValue({
      items: [
        {
          id: 1,
          name: 'infra',
          full_name: 'WebbPulse/infra',
          private: true,
          html_url: 'https://github.com/WebbPulse/infra',
          default_branch: 'main',
        },
      ],
    });

    renderPage();

    const toggle = await screen.findByRole('button', {
      name: /^Repositories in WebbPulse/,
    });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveTextContent('1 selected');
    expect(apiMock.listGitHubRepositories).not.toHaveBeenCalled();
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    const list = await screen.findByRole('list', {
      name: 'Repositories for installation 77',
    });
    expect(
      within(list).getByRole('link', { name: 'WebbPulse/infra' })
    ).toHaveAttribute('href', 'https://github.com/WebbPulse/infra');
    expect(within(list).getByText(/Private/)).toBeInTheDocument();
    expect(apiMock.listGitHubRepositories.mock.calls[0]?.[0]).toBe('77');
  });

  it('says so when an installation reaches no repositories', async () => {
    apiMock.getGitHubApp.mockResolvedValue(anApp());
    apiMock.listGitHubInstallations.mockResolvedValue({
      items: [anInstallation()],
    });
    apiMock.listGitHubRepositories.mockResolvedValue({ items: [] });

    renderPage();

    await userEvent.click(
      await screen.findByRole('button', { name: /^Repositories in WebbPulse/ })
    );
    expect(
      await screen.findByText('This installation reaches no repositories.')
    ).toBeInTheDocument();
  });
});
