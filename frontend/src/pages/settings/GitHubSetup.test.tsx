import { screen } from '@testing-library/react';
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
import { anInstallation } from './fixtures';

vi.mock('../../api/client', () => apiClientModuleMock());

const { GitHubSetup } = await import('./GitHubSetup');

/** Mounts the setup callback at `url`, in strict mode as development runs it. */
function renderAt(url: string): void {
  renderWithAuth(
    <StrictMode>
      <Routes>
        <Route path="/settings/github/setup" element={<GitHubSetup />} />
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
    apiMock.recordGitHubInstallation.mockResolvedValue(anInstallation());

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
  });

  it('calls nothing when the link has no installation', async () => {
    renderAt('/settings/github/setup');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'missing its installation'
    );
    expect(apiMock.recordGitHubInstallation).not.toHaveBeenCalled();
  });
});
