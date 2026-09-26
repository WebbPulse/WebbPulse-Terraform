import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode } from 'react';
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
import { anApp } from './fixtures';

vi.mock('../../api/client', () => apiClientModuleMock());
vi.mock('./githubNavigation', () => ({ postManifest: vi.fn(), goTo: vi.fn() }));

const navigation = await import('./githubNavigation');
const { GitHubCreated } = await import('./GitHubCreated');

/** Mounts the create callback at `url`, in strict mode as development runs it. */
function renderAt(url: string): void {
  renderWithAuth(
    <StrictMode>
      <Routes>
        <Route path="/settings/github/created" element={<GitHubCreated />} />
      </Routes>
    </StrictMode>,
    signedInAuthClient(),
    [url]
  );
}

describe('GitHubCreated', () => {
  beforeEach(() => {
    resetApiMock();
    vi.mocked(navigation.goTo).mockReset();
  });

  it('exchanges the code once and shows the finish setup step', async () => {
    apiMock.convertGitHubManifest.mockResolvedValue(anApp());

    renderAt('/settings/github/created?code=c1&state=s1');

    expect(
      await screen.findByText(/is created and its credentials are stored/)
    ).toBeInTheDocument();
    expect(apiMock.convertGitHubManifest).toHaveBeenCalledTimes(1);
    expect(apiMock.convertGitHubManifest).toHaveBeenCalledWith({
      code: 'c1',
      state: 's1',
    });
    expect(
      screen.getByRole('link', { name: 'Open App settings' })
    ).toHaveAttribute('href', anApp().settings_url);
    const download = screen.getByRole('link', { name: 'Download logo' });
    expect(download).toHaveAttribute('href', '/github-app-logo.png');
    expect(download).toHaveAttribute(
      'download',
      'webbpulse-terraform-github-app.png'
    );
    expect(screen.getByText('#4d9fff')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Copy badge background' })
    ).toBeInTheDocument();
  });

  it('goes on to install from the finish setup step', async () => {
    apiMock.convertGitHubManifest.mockResolvedValue(anApp());
    apiMock.startGitHubInstall.mockResolvedValue({
      install_url: 'https://github.com/apps/x/installations/new?state=s2',
      state: 's2',
    });

    renderAt('/settings/github/created?code=c1&state=s1');

    await userEvent.click(
      await screen.findByRole('button', { name: 'Install on repositories' })
    );
    expect(navigation.goTo).toHaveBeenCalledWith(
      'https://github.com/apps/x/installations/new?state=s2'
    );
  });

  it('shows why a rejected code failed', async () => {
    apiMock.convertGitHubManifest.mockRejectedValue(
      new Error('GitHub did not accept that code.')
    );

    renderAt('/settings/github/created?code=c1&state=s1');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'GitHub did not accept that code.'
    );
    expect(
      screen.getByRole('link', { name: 'Back to GitHub settings' })
    ).toBeInTheDocument();
  });

  it('calls nothing when the link has no code', async () => {
    renderAt('/settings/github/created');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'missing its code'
    );
    expect(apiMock.convertGitHubManifest).not.toHaveBeenCalled();
  });
});
