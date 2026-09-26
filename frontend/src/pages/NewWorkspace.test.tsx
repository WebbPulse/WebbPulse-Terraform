import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { aFreshWorkspace } from '../test-helpers/fixtures';
import {
  jsonResponse,
  renderWithAuth,
  signedInAuthClient,
  stubAuthClient,
} from '../test-helpers/renderWithAuth';
import { anApp, anInstallation } from './settings/fixtures';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../test-helpers/apiMock';

vi.mock('../api/client', () => apiClientModuleMock());

const { NewWorkspace } = await import('./NewWorkspace');

/** An auth client for an admin, who can read the GitHub routes. */
function adminClient(): ReturnType<typeof stubAuthClient> {
  return stubAuthClient({
    fetch: () =>
      Promise.resolve(
        jsonResponse({ access_token: 'test-token', expires_in: 3600 })
      ),
    user: { email: 'admin@webbpulse.com', is_admin: true },
  });
}

/** Mounts the page with the list and a detail route beside it. */
function renderPage(admin = false): void {
  renderWithAuth(
    <Routes>
      <Route path="/workspaces" element={<p>Workspace list</p>} />
      <Route path="/workspaces/new" element={<NewWorkspace />} />
      <Route path="/workspaces/:workspaceId" element={<p>Detail page</p>} />
    </Routes>,
    admin ? adminClient() : signedInAuthClient(),
    ['/workspaces/new']
  );
}

/** Stubs one installation carrying the given repositories. */
function stubRepositories(): void {
  apiMock.getGitHubApp.mockResolvedValue(anApp());
  apiMock.listGitHubInstallations.mockResolvedValue({
    items: [anInstallation()],
  });
  apiMock.listGitHubRepositories.mockResolvedValue({
    items: [
      {
        id: 1,
        name: 'infra',
        full_name: 'WebbPulse/infra',
        private: true,
        default_branch: 'staging',
      },
      {
        id: 2,
        name: 'site',
        full_name: 'WebbPulse/site',
        private: false,
        default_branch: 'main',
      },
    ],
  });
}

/** The step marked current in the indicator. */
function currentStep(): HTMLElement {
  const nav = screen.getByRole('navigation', { name: 'Steps' });
  const current = nav.querySelector('[aria-current="step"]');
  if (!(current instanceof HTMLElement)) {
    throw new Error('No current step.');
  }
  return current;
}

describe('NewWorkspace', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('opens on the workflow step with version control first', () => {
    renderPage();

    expect(
      screen.getByRole('heading', { name: 'Create a new workspace' })
    ).toBeInTheDocument();
    expect(currentStep()).toHaveTextContent('Choose your workflow');
    const tiles = screen
      .getAllByRole('button')
      .filter((button) => button.textContent.includes('workflow'));
    expect(tiles.map((tile) => tile.textContent)).toEqual([
      expect.stringContaining('Version control workflow'),
      expect.stringContaining('CLI-driven workflow'),
      expect.stringContaining('API-driven workflow'),
    ]);
    expect(
      screen.queryByRole('form', { name: 'Create a workspace' })
    ).not.toBeInTheDocument();
  });

  it('takes the CLI workflow straight to settings in two steps', async () => {
    renderPage();

    await userEvent.click(
      screen.getByRole('button', { name: /CLI-driven workflow/ })
    );

    expect(currentStep()).toHaveTextContent('Configure settings');
    const nav = screen.getByRole('navigation', { name: 'Steps' });
    expect(within(nav).getAllByRole('listitem')).toHaveLength(2);
    expect(
      screen.getByRole('form', { name: 'Create a workspace' })
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Workspace name')).toHaveFocus();
  });

  it('keeps Create disabled with a hint until a name is entered, then creates and opens it', async () => {
    apiMock.createWorkspace.mockResolvedValue(aFreshWorkspace());
    renderPage();

    await userEvent.click(
      screen.getByRole('button', { name: /API-driven workflow/ })
    );
    const create = screen.getByRole('button', { name: 'Create workspace' });
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription('Enter a workspace name.');

    await userEvent.type(screen.getByLabelText('Workspace name'), '-bad');
    expect(create).toHaveAccessibleDescription(/has to start with a letter/);

    await userEvent.clear(screen.getByLabelText('Workspace name'));
    await userEvent.type(screen.getByLabelText('Workspace name'), 'platform');
    expect(create).toBeEnabled();
    await userEvent.click(create);

    expect(apiMock.createWorkspace).toHaveBeenCalledWith({
      name: 'platform',
      engine: 'terraform',
      engine_version: '1.16.3',
    });
    expect(await screen.findByText('Detail page')).toBeInTheDocument();
  });

  it('sends a description when one is given', async () => {
    apiMock.createWorkspace.mockResolvedValue(aFreshWorkspace());
    renderPage();

    await userEvent.click(
      screen.getByRole('button', { name: /CLI-driven workflow/ })
    );
    await userEvent.type(screen.getByLabelText('Workspace name'), 'platform');
    await userEvent.type(
      screen.getByLabelText('Description'),
      'The platform workspace.'
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Create workspace' })
    );

    expect(apiMock.createWorkspace).toHaveBeenCalledWith({
      name: 'platform',
      engine: 'terraform',
      engine_version: '1.16.3',
      description: 'The platform workspace.',
    });
  });

  it('defaults the engine version to the newest listed and lets another be typed', async () => {
    apiMock.createWorkspace.mockResolvedValue(aFreshWorkspace());
    renderPage();

    await userEvent.click(
      screen.getByRole('button', { name: /CLI-driven workflow/ })
    );
    await userEvent.type(screen.getByLabelText('Workspace name'), 'platform');
    await userEvent.click(
      screen.getByRole('button', { name: /Advanced options/ })
    );
    const version = screen.getByLabelText('Engine version');
    expect(version).toHaveValue('1.16.3');
    expect(
      within(version).getByRole('option', { name: '1.16.3 (default)' })
    ).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText('Engine'), 'tofu');
    expect(version).toHaveValue('1.12.6');

    await userEvent.selectOptions(version, 'Other version');
    const typed = screen.getByLabelText('Specific version');
    await userEvent.clear(typed);
    await userEvent.type(typed, '1.12');
    const create = screen.getByRole('button', { name: 'Create workspace' });
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription(
      'The engine version has to be an exact release, such as 1.16.3.'
    );

    await userEvent.type(typed, '.7');
    await userEvent.click(create);

    expect(apiMock.createWorkspace).toHaveBeenCalledWith({
      name: 'platform',
      engine: 'tofu',
      engine_version: '1.12.7',
    });
  });

  it('walks version control through the repository step and creates from it', async () => {
    stubRepositories();
    apiMock.createWorkspace.mockResolvedValue(aFreshWorkspace());
    renderPage(true);

    await userEvent.click(
      screen.getByRole('button', { name: /Version control workflow/ })
    );

    expect(currentStep()).toHaveTextContent('Connect to a repository');
    const infra = await screen.findByRole('option', {
      name: /WebbPulse\/infra/,
    });
    expect(infra).toHaveTextContent('Private');
    expect(infra).toHaveTextContent('default branch staging');
    expect(
      screen.getByRole('option', { name: /WebbPulse\/site/ })
    ).toHaveTextContent('Public');
    expect(
      screen.getByRole('link', { name: 'Manage the GitHub App' })
    ).toHaveAttribute('href', '/settings/github');

    await userEvent.click(infra);

    expect(currentStep()).toHaveTextContent('Configure settings');
    expect(screen.getByLabelText('Workspace name')).toHaveValue('infra');
    expect(screen.getByText('WebbPulse/infra')).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole('button', { name: /Advanced options/ })
    );
    expect(screen.getByLabelText('VCS branch')).toHaveValue('staging');
    await userEvent.type(
      screen.getByLabelText('Working directory'),
      'examples/first-run'
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Create workspace' })
    );

    expect(apiMock.createWorkspace).toHaveBeenCalledWith({
      name: 'infra',
      engine: 'terraform',
      engine_version: '1.16.3',
      vcs_repo: 'WebbPulse/infra',
      tracked_branch: 'staging',
      working_directory: 'examples/first-run',
      file_triggers_enabled: true,
      trigger_patterns: [],
      speculative_plans: true,
    });
    expect(await screen.findByText('Detail page')).toBeInTheDocument();
  });

  it('starts reading repositories on arrival and shows labelled placeholders meanwhile', async () => {
    apiMock.getGitHubApp.mockReturnValue(new Promise(() => undefined));
    apiMock.listGitHubInstallations.mockReturnValue(
      new Promise(() => undefined)
    );
    renderPage(true);

    await vi.waitFor(() => {
      expect(apiMock.listGitHubInstallations).toHaveBeenCalled();
    });
    expect(apiMock.getGitHubApp).toHaveBeenCalled();

    await userEvent.click(
      screen.getByRole('button', { name: /Version control workflow/ })
    );

    expect(
      screen.getByRole('status', { name: 'Loading repositories' })
    ).toBeInTheDocument();
    expect(
      screen.getByText('Loading repositories from GitHub')
    ).toBeInTheDocument();
  });

  it('keeps a typed name when a repository is picked afterwards', async () => {
    stubRepositories();
    renderPage(true);

    await userEvent.click(
      screen.getByRole('button', { name: /Version control workflow/ })
    );
    await userEvent.click(
      await screen.findByRole('option', { name: /WebbPulse\/infra/ })
    );
    await userEvent.clear(screen.getByLabelText('Workspace name'));
    await userEvent.type(screen.getByLabelText('Workspace name'), 'custom');
    await userEvent.click(
      screen.getByRole('button', { name: 'Change repository' })
    );
    await userEvent.click(
      screen.getByRole('option', { name: /WebbPulse\/site/ })
    );

    expect(screen.getByLabelText('Workspace name')).toHaveValue('custom');
    expect(screen.getByText('WebbPulse/site')).toBeInTheDocument();
  });

  it('goes back a step at a time and jumps back from a finished step', async () => {
    stubRepositories();
    renderPage(true);

    await userEvent.click(
      screen.getByRole('button', { name: /Version control workflow/ })
    );
    await userEvent.click(
      await screen.findByRole('option', { name: /WebbPulse\/infra/ })
    );
    expect(currentStep()).toHaveTextContent('Configure settings');

    await userEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(currentStep()).toHaveTextContent('Connect to a repository');

    await userEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(currentStep()).toHaveTextContent('Choose your workflow');

    await userEvent.click(
      screen.getByRole('button', { name: /Version control workflow/ })
    );
    await userEvent.click(
      screen.getByRole('option', { name: /WebbPulse\/infra/ })
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Choose your workflow, done' })
    );
    expect(currentStep()).toHaveTextContent('Choose your workflow');
    expect(apiMock.createWorkspace).not.toHaveBeenCalled();
  });

  it('cancels back to the list without creating anything', async () => {
    renderPage();

    const cancel = screen.getByRole('link', { name: 'Cancel' });
    expect(cancel).toHaveAttribute('href', '/workspaces');
    await userEvent.click(cancel);

    expect(await screen.findByText('Workspace list')).toBeInTheDocument();
    expect(apiMock.createWorkspace).not.toHaveBeenCalled();
  });

  it('tells a non-admin why no repository can be connected, without reading GitHub', async () => {
    renderPage();

    await userEvent.click(
      screen.getByRole('button', { name: /Version control workflow/ })
    );

    expect(
      screen.getByText(/Only an admin can connect a repository/)
    ).toBeInTheDocument();
    expect(apiMock.getGitHubApp).not.toHaveBeenCalled();
    expect(apiMock.listGitHubInstallations).not.toHaveBeenCalled();
  });
});
