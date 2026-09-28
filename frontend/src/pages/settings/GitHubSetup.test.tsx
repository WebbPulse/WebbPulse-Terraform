import { screen, within, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes, useLocation } from 'react-router-dom';

import {
  renderWithAuth,
  signedInAuthClient,
} from '../../test-helpers/renderWithAuth';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../../test-helpers/apiMock';
import { anInstallation } from './fixtures';

vi.mock('../../api/client', () => apiClientModuleMock());

const { GitHubSetup } = await import('./GitHubSetup');

/** Shows the router's current URL, so a test can see the query was cleared. */
function LocationProbe(): React.ReactElement {
  const location = useLocation();
  return (
    <span data-testid="location">{location.pathname + location.search}</span>
  );
}

/** Mounts the setup callback at `url`, in strict mode as development runs it. */
function renderAt(url: string): void {
  renderWithAuth(
    <StrictMode>
      <Routes>
        <Route
          path="/settings/github/setup"
          element={
            <>
              <GitHubSetup />
              <LocationProbe />
            </>
          }
        />
      </Routes>
    </StrictMode>,
    signedInAuthClient(),
    [url]
  );
}

describe('GitHubSetup', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('forwards the callback once and shows the installation', async () => {
    apiMock.recordGitHubInstallation.mockResolvedValue(
      anInstallation({ repository_count: 1 })
    );
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

    renderAt(
      '/settings/github/setup?installation_id=77&setup_action=install&state=s2'
    );

    expect(
      await screen.findByText('Installed on WebbPulse.')
    ).toBeInTheDocument();
    expect(apiMock.recordGitHubInstallation).toHaveBeenCalledTimes(1);
    expect(apiMock.recordGitHubInstallation).toHaveBeenCalledWith({
      installation_id: 77,
      setup_action: 'install',
      state: 's2',
    });
    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent(
        /^\/settings\/github\/setup$/
      );
    });

    const repositories = screen.getByRole('region', { name: 'Repositories' });
    expect(repositories).toHaveTextContent('1 selected');
    expect(
      await within(repositories).findByRole('link', { name: 'WebbPulse/infra' })
    ).toBeInTheDocument();
    expect(apiMock.listGitHubRepositories.mock.calls[0]?.[0]).toBe('77');

    const next = screen.getByRole('region', { name: 'Next step' });
    expect(next).toHaveTextContent(
      'Workspaces can now be connected to these repositories.'
    );
    expect(
      within(next).getByRole('link', { name: 'Back to GitHub settings' })
    ).toHaveAttribute('href', '/settings/github');
  });

  it('says an update was applied', async () => {
    apiMock.recordGitHubInstallation.mockResolvedValue(
      anInstallation({ repository_selection: 'all' })
    );
    apiMock.listGitHubRepositories.mockResolvedValue({ items: [] });

    renderAt('/settings/github/setup?installation_id=77&setup_action=update');

    expect(
      await screen.findByText('Updated the installation on WebbPulse.')
    ).toBeInTheDocument();
    expect(
      screen.getByText('It can reach every repository in the account.')
    ).toBeInTheDocument();
  });

  it('shows why an installation was refused', async () => {
    apiMock.recordGitHubInstallation.mockRejectedValue(
      new Error('That installation is not of this App.')
    );

    renderAt(
      '/settings/github/setup?installation_id=99&setup_action=install&state=s2'
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'not of this App'
    );
  });

  it('explains a requested install without calling the API', async () => {
    renderAt('/settings/github/setup?setup_action=request');

    expect(await screen.findByRole('status')).toHaveTextContent(
      'An owner of the account has to approve it'
    );
    expect(apiMock.recordGitHubInstallation).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent(
        /^\/settings\/github\/setup$/
      );
    });
  });

  it('calls nothing when the link has no installation', async () => {
    renderAt('/settings/github/setup');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'missing its installation'
    );
    expect(apiMock.recordGitHubInstallation).not.toHaveBeenCalled();
  });
});
