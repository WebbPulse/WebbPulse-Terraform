import { screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { aConfigVersion } from '../../test-helpers/fixtures';
import {
  renderWithAuth,
  signedInAuthClient,
} from '../../test-helpers/renderWithAuth';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../../test-helpers/apiMock';

vi.mock('../../api/client', () => apiClientModuleMock());

const { WorkspaceReadme } = await import('./WorkspaceReadme');

const WORKSPACE = 'ws-01J000000000000000000000';
const VCS = { repo: 'acme/infra', sha: 'abc123', branch: 'main' };

/** Mounts the card over the given versions. */
function renderReadme(
  versions = [aConfigVersion({ source: 'vcs', vcs: VCS })],
  workingDirectory = ''
): void {
  renderWithAuth(
    <WorkspaceReadme
      workspaceId={WORKSPACE}
      versions={versions}
      workingDirectory={workingDirectory}
    />,
    signedInAuthClient()
  );
}

describe('WorkspaceReadme', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('renders the README of the latest version with links on GitHub', async () => {
    apiMock.getConfigVersion.mockResolvedValue({
      ...aConfigVersion({ source: 'vcs', vcs: VCS }),
      readme: {
        path: 'README.md',
        content: '# Network\n\nSee [usage](docs/usage.md).\n',
        truncated: false,
      },
    });
    renderReadme();

    const card = await screen.findByTestId('workspace-readme-card');
    expect(
      within(card).getByRole('heading', { name: 'Network' })
    ).toBeInTheDocument();
    expect(within(card).getByRole('link', { name: 'usage' })).toHaveAttribute(
      'href',
      'https://github.com/acme/infra/blob/abc123/docs/usage.md'
    );
    expect(
      within(card).getByRole('link', { name: 'View on GitHub' })
    ).toHaveAttribute(
      'href',
      'https://github.com/acme/infra/blob/abc123/README.md'
    );
    expect(apiMock.getConfigVersion).toHaveBeenCalledWith(
      WORKSPACE,
      'cv-01J000000000000000000000',
      expect.anything()
    );
  });

  it('drops raw HTML from the source', async () => {
    apiMock.getConfigVersion.mockResolvedValue({
      ...aConfigVersion(),
      readme: {
        path: 'README.md',
        content: 'Hello <script>alert(1)</script><b>bold</b>\n',
        truncated: false,
      },
    });
    renderReadme([aConfigVersion()]);

    const body = await screen.findByTestId('workspace-readme');
    expect(body.querySelector('script')).toBeNull();
    expect(body.querySelector('b')).toBeNull();
  });

  it('shows relative links as text for an API upload', async () => {
    apiMock.getConfigVersion.mockResolvedValue({
      ...aConfigVersion(),
      readme: {
        path: 'README.md',
        content: 'See [usage](docs/usage.md).\n',
        truncated: false,
      },
    });
    renderReadme([aConfigVersion()]);

    const body = await screen.findByTestId('workspace-readme');
    expect(within(body).getByText('usage')).toBeInTheDocument();
    expect(within(body).queryByRole('link')).toBeNull();
    expect(screen.queryByRole('link', { name: 'View on GitHub' })).toBeNull();
  });

  it('says when the README was cut short', async () => {
    apiMock.getConfigVersion.mockResolvedValue({
      ...aConfigVersion({ source: 'vcs', vcs: VCS }),
      readme: { path: 'README.md', content: '# Long\n', truncated: true },
    });
    renderReadme();

    const note = await screen.findByTestId('workspace-readme-truncated');
    expect(
      within(note).getByRole('link', { name: 'Read the rest on GitHub' })
    ).toBeInTheDocument();
  });

  it('leaves a subtle hint when there is no README', async () => {
    apiMock.getConfigVersion.mockResolvedValue({
      ...aConfigVersion(),
      readme: null,
    });
    renderReadme([aConfigVersion()], 'stacks/app/');

    const hint = await screen.findByTestId('workspace-readme-hint');
    expect(hint).toHaveTextContent(
      'Add a README.md to stacks/app to describe this workspace here.'
    );
    expect(screen.queryByTestId('workspace-readme-card')).toBeNull();
  });

  it('renders nothing and fetches nothing with no uploaded version', () => {
    renderReadme([aConfigVersion({ status: 'pending' })]);

    expect(screen.queryByTestId('workspace-readme-card')).toBeNull();
    expect(screen.queryByTestId('workspace-readme-hint')).toBeNull();
    expect(apiMock.getConfigVersion).not.toHaveBeenCalled();
  });
});
