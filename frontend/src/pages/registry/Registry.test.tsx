import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import {
  jsonResponse,
  renderWithAuth,
  signedInAuthClient,
  stubAuthClient,
} from '../../test-helpers/renderWithAuth';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../../test-helpers/apiMock';
import { anApp, anInstallation } from '../settings/fixtures';
import { aModule, aVersionDetail, someDocs } from './fixtures';

vi.mock('../../api/client', () => apiClientModuleMock());

const { ConnectModule, ModuleDetail, Registry } = await import('./index');

/** An auth client for an admin, who may connect, resync and delete. */
function adminClient(): ReturnType<typeof stubAuthClient> {
  return stubAuthClient({
    fetch: () =>
      Promise.resolve(
        jsonResponse({ access_token: 'test-token', expires_in: 3600 })
      ),
    user: { email: 'admin@webbpulse.com', is_admin: true },
  });
}

/** Mounts the registry routes at one path. */
function renderAt(path: string, admin = false): void {
  renderWithAuth(
    <Routes>
      <Route path="/registry" element={<Registry />} />
      <Route path="/registry/new" element={<ConnectModule />} />
      <Route
        path="/registry/:namespace/:name/:provider"
        element={<ModuleDetail />}
      />
    </Routes>,
    admin ? adminClient() : signedInAuthClient(),
    [path]
  );
}

describe('Registry list', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('lists each module with its newest published version and repository', async () => {
    apiMock.listModules.mockResolvedValue({ modules: [aModule()] });

    renderAt('/registry');

    const link = await screen.findByRole('link', { name: 'network' });
    expect(link).toHaveAttribute('href', '/registry/WebbPulse/network/aws');
    const row = link.closest('tr');
    if (row === null) {
      throw new Error('No row.');
    }
    expect(within(row).getByText('1.2.0')).toBeInTheDocument();
    expect(within(row).getByText('Failed')).toBeInTheDocument();
    expect(
      within(row).getByText('WebbPulse/terraform-aws-network')
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: 'Connect module' })
    ).not.toBeInTheDocument();
  });

  it('invites an admin to connect the first module', async () => {
    apiMock.listModules.mockResolvedValue({ modules: [] });

    renderAt('/registry', true);

    expect(await screen.findByText('No modules yet.')).toBeInTheDocument();
    expect(
      screen.getAllByRole('link', { name: 'Connect module' })[0]
    ).toHaveAttribute('href', '/registry/new');
  });

  it('filters modules by address', async () => {
    apiMock.listModules.mockResolvedValue({
      modules: [
        aModule(),
        aModule({ name: 'dns', source: 'WebbPulse/dns/aws', versions: [] }),
      ],
    });

    renderAt('/registry');

    await screen.findByRole('link', { name: 'dns' });
    await userEvent.type(screen.getByLabelText('Filter modules'), 'net');
    expect(screen.queryByRole('link', { name: 'dns' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'network' })).toBeInTheDocument();
  });
});

describe('Module page', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('opens on the newest published version with its readme and usage snippet', async () => {
    apiMock.getModule.mockResolvedValue(aModule());
    apiMock.getModuleVersion.mockResolvedValue(aVersionDetail());

    renderAt('/registry/WebbPulse/network/aws');

    expect(
      await screen.findByRole('heading', { name: 'Network' })
    ).toBeInTheDocument();
    expect(apiMock.getModuleVersion).toHaveBeenCalledWith(
      'WebbPulse',
      'network',
      'aws',
      '1.2.0',
      expect.anything()
    );
    expect(screen.getByTestId('module-readme').innerHTML).not.toContain(
      'script'
    );
    const snippet = screen
      .getAllByTestId('code-block')
      .find((block) => block.dataset['subject'] === 'module usage');
    expect(snippet?.textContent).toContain(
      `source  = "${window.location.host}/WebbPulse/network/aws"`
    );
    expect(snippet?.textContent).toContain('version = "1.2.0"');
    expect(snippet?.textContent).toContain('cidr = # required');
    expect(screen.getByLabelText('Version')).toHaveValue('1.2.0');
  });

  it('tabs through inputs, outputs, resources, providers and submodules', async () => {
    apiMock.getModule.mockResolvedValue(aModule());
    apiMock.getModuleVersion.mockResolvedValue(aVersionDetail());

    renderAt('/registry/WebbPulse/network/aws');
    await screen.findByRole('heading', { name: 'Network' });

    await userEvent.click(screen.getByRole('tab', { name: /Inputs/ }));
    expect(screen.getByText('map(string)')).toBeInTheDocument();
    expect(screen.getByText('required')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: /Outputs/ }));
    expect(screen.getByText('vpc_id')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: /Resources/ }));
    expect(screen.getByText('aws_vpc')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: /Providers/ }));
    expect(screen.getByText('hashicorp/aws')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: /Submodules/ }));
    const submodule = screen.getByRole('region', {
      name: 'Submodule subnets',
    });
    expect(within(submodule).getByTestId('code-block').textContent).toContain(
      '/WebbPulse/network/aws//modules/subnets"'
    );
  });

  it('shows why a failed version has no documentation', async () => {
    apiMock.getModule.mockResolvedValue(aModule());

    renderAt('/registry/WebbPulse/network/aws?version=1.3.0');

    expect(
      await screen.findByText('Version 1.3.0 failed to publish.')
    ).toBeInTheDocument();
    expect(
      screen.getByText('The tag has no .tf file at the repository root.')
    ).toBeInTheDocument();
    expect(apiMock.getModuleVersion).not.toHaveBeenCalled();
  });

  it('switches version from the selector', async () => {
    apiMock.getModule.mockResolvedValue(aModule());
    apiMock.getModuleVersion.mockResolvedValue(aVersionDetail());

    renderAt('/registry/WebbPulse/network/aws');
    await screen.findByRole('heading', { name: 'Network' });

    await userEvent.selectOptions(screen.getByLabelText('Version'), '1.3.0');
    expect(
      await screen.findByText('Version 1.3.0 failed to publish.')
    ).toBeInTheDocument();
  });

  it('warns about files that could not be read', async () => {
    apiMock.getModule.mockResolvedValue(aModule());
    apiMock.getModuleVersion.mockResolvedValue(
      aVersionDetail({ docs: someDocs({ parse_errors: ['broken.tf'] }) })
    );

    renderAt('/registry/WebbPulse/network/aws');

    expect(await screen.findByText('broken.tf')).toBeInTheDocument();
  });

  it('lets an admin resync the repository tags', async () => {
    apiMock.getModule.mockResolvedValue(aModule());
    apiMock.getModuleVersion.mockResolvedValue(aVersionDetail());
    apiMock.resyncModule.mockResolvedValue({
      source: 'WebbPulse/network/aws',
      delivery: 'queued',
    });

    renderAt('/registry/WebbPulse/network/aws', true);

    await userEvent.click(
      await screen.findByRole('button', { name: 'Resync' })
    );
    expect(
      await screen.findByRole('button', { name: 'Resync queued' })
    ).toBeInTheDocument();
    expect(apiMock.resyncModule).toHaveBeenCalledWith(
      'WebbPulse',
      'network',
      'aws'
    );
  });

  it('hides resync and delete from a non-admin', async () => {
    apiMock.getModule.mockResolvedValue(aModule());
    apiMock.getModuleVersion.mockResolvedValue(aVersionDetail());

    renderAt('/registry/WebbPulse/network/aws');
    await screen.findByRole('heading', { name: 'Network' });

    expect(
      screen.queryByRole('button', { name: 'Resync' })
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Delete module' })
    ).not.toBeInTheDocument();
  });

  it('deletes the module after confirming and returns to the list', async () => {
    apiMock.getModule.mockResolvedValue(aModule());
    apiMock.getModuleVersion.mockResolvedValue(aVersionDetail());
    apiMock.deleteModule.mockResolvedValue(undefined);
    apiMock.listModules.mockResolvedValue({ modules: [] });

    renderAt('/registry/WebbPulse/network/aws', true);

    await userEvent.click(
      await screen.findByRole('button', { name: 'Delete module' })
    );
    const dialog = screen.getByRole('dialog');
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Delete module' })
    );
    expect(await screen.findByText('No modules yet.')).toBeInTheDocument();
    expect(apiMock.deleteModule).toHaveBeenCalledWith(
      'WebbPulse',
      'network',
      'aws'
    );
  });
});

describe('Connect module', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getGitHubApp.mockResolvedValue(anApp());
    apiMock.listGitHubInstallations.mockResolvedValue({
      items: [anInstallation()],
    });
    apiMock.listGitHubRepositories.mockResolvedValue({
      items: [
        {
          id: 1,
          name: 'terraform-aws-network',
          full_name: 'WebbPulse/terraform-aws-network',
          private: true,
          default_branch: 'main',
        },
        {
          id: 2,
          name: 'infra',
          full_name: 'WebbPulse/infra',
          private: false,
          default_branch: 'main',
        },
      ],
    });
  });

  it('connects a conventionally named repository with the derived address', async () => {
    apiMock.createModule.mockResolvedValue(aModule({ versions: [] }));
    apiMock.getModule.mockResolvedValue(aModule({ versions: [] }));

    renderAt('/registry/new', true);

    await userEvent.click(
      await screen.findByRole('option', {
        name: /WebbPulse\/terraform-aws-network/,
      })
    );
    expect(screen.getByTestId('connect-address')).toHaveTextContent(
      'WebbPulse/network/aws'
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Connect module' })
    );

    expect(apiMock.createModule).toHaveBeenCalledWith({
      vcs_repo: 'WebbPulse/terraform-aws-network',
      import_tags: true,
    });
    expect(
      await screen.findByText('No version is published yet.', { exact: false })
    ).toBeInTheDocument();
  });

  it('needs a name and provider for any other repository', async () => {
    apiMock.createModule.mockResolvedValue(
      aModule({ name: 'infra', provider: 'null', versions: [] })
    );

    renderAt('/registry/new', true);

    await userEvent.click(
      await screen.findByRole('option', { name: /WebbPulse\/infra/ })
    );
    const connect = screen.getByRole('button', { name: 'Connect module' });
    expect(connect).toBeDisabled();

    await userEvent.type(screen.getByLabelText('Module name'), 'infra');
    await userEvent.type(screen.getByLabelText('Provider'), 'Null');
    expect(
      screen.getByText(
        'Lowercase letters and digits only, such as aws or null.'
      )
    ).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText('Provider'));
    await userEvent.type(screen.getByLabelText('Provider'), 'null');
    await userEvent.click(
      screen.getByLabelText('Import existing version tags', { exact: false })
    );
    await userEvent.click(connect);

    expect(apiMock.createModule).toHaveBeenCalledWith({
      vcs_repo: 'WebbPulse/infra',
      import_tags: false,
      name: 'infra',
      provider: 'null',
    });
  });

  it('tells a non-admin only an admin can connect a repository', async () => {
    renderAt('/registry/new');

    expect(
      await screen.findByText(/Only an admin can connect a repository/)
    ).toBeInTheDocument();
  });
});
