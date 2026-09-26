import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { invalidateQueries } from '@webbpulse/api-client/react';
import { ApiError } from '@webbpulse/api-client';
import userEvent from '@testing-library/user-event';
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type Mock,
} from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { Layout } from '../components/Layout';

import {
  aConfigVersion,
  aFreshWorkspace,
  aRun,
  aVariable,
  aWorkspace,
} from '../test-helpers/fixtures';
import { runRolePrefix } from '../api/runRoleSetup';
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

const { workspaceRoutes } = await import('./workspaceRoutes');

/** The role name prefix the fixture's runner may assume. */
function assumablePrefix(): string {
  return runRolePrefix(aWorkspace().run_role_setup.role_name);
}

/** Mounts the workspace pages inside the shell at the given path. */
/** What the check answers while no run has tried the role. */
const UNVERIFIED = {
  connected: false,
  status: 'unverified' as const,
  account_id: null,
  error: 'No run has assumed this role yet.',
  run_id: null,
  checked_at: null,
};

/** What the check answers once a run has assumed the role. */
const CONNECTED = {
  connected: true,
  status: 'connected' as const,
  account_id: '123456789012',
  error: null,
  run_id: 'run-01J000000000000000000000',
  checked_at: '2026-09-17T00:05:00Z',
};

/** The settings page that holds the run role form once an ARN is saved. */
const RUN_ROLE_SETTINGS =
  '/workspaces/ws-01J000000000000000000000/settings/run-role';

function renderDetail(path = '/workspaces/ws-01J000000000000000000000'): void {
  renderWithAuth(
    <Routes>
      <Route element={<Layout />}>{workspaceRoutes()}</Route>
    </Routes>,
    signedInAuthClient(),
    [path]
  );
}

/** The first link in the rail with this name. */
function railLink(name: string): HTMLElement {
  const link = screen.getAllByRole('link', { name })[0];
  if (link === undefined) {
    throw new Error(`No link named ${name}`);
  }
  return link;
}

describe('WorkspaceLayout', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.readRunRoleCheck.mockResolvedValue(UNVERIFIED);
    apiMock.listVariables.mockResolvedValue({
      items: [
        aVariable(),
        aVariable({ key: 'token', sensitive: true, value: null }),
      ],
    });
    apiMock.listConfigVersions.mockResolvedValue({
      items: [aConfigVersion()],
    });
    apiMock.listRuns.mockResolvedValue({ items: [aRun('planned')] });
  });

  it('opens on the overview with the latest run and the workspace rail', async () => {
    renderDetail();

    expect(
      await screen.findByRole('heading', { name: 'platform' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: 'Latest run' })
    ).toBeInTheDocument();
    expect(await screen.findByTestId('run-state-badge')).toHaveAttribute(
      'data-state',
      'planned'
    );
    expect(railLink('Overview')).toHaveAttribute('aria-current', 'page');
  });

  it('swaps the rail for the workspace sections', async () => {
    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });
    expect(
      screen.getAllByRole('navigation', { name: 'Workspace sections' })
    ).not.toHaveLength(0);
    for (const label of [
      'Overview',
      'Runs',
      'Configuration versions',
      'Variables',
      'Settings',
    ]) {
      expect(railLink(label)).toBeInTheDocument();
    }
    expect(railLink('Workspaces')).toHaveAttribute('href', '/workspaces');
  });

  it('patches the editable fields without the name or the run role', async () => {
    apiMock.updateWorkspace.mockResolvedValue(aWorkspace());

    renderDetail('/workspaces/ws-01J000000000000000000000/settings/general');

    await screen.findByRole('form', { name: 'Workspace settings' });
    await userEvent.selectOptions(
      screen.getByLabelText('Engine version'),
      '1.15.9'
    );
    await userEvent.click(
      screen.getByRole('button', { name: 'Save settings' })
    );

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(
      'ws-01J000000000000000000000',
      {
        description: 'The platform workspace.',
        engine: 'terraform',
        engine_version: '1.15.9',
        working_directory: 'terraform',
      }
    );
  });

  it('keeps a stored version off the list as a typed one', async () => {
    apiMock.updateWorkspace.mockResolvedValue(aWorkspace());

    renderDetail('/workspaces/ws-01J000000000000000000000/settings/general');

    await screen.findByRole('form', { name: 'Workspace settings' });
    expect(screen.getByLabelText('Engine version')).toHaveValue('__other__');
    const typed = screen.getByLabelText('Specific version');
    expect(typed).toHaveValue('1.11.0');
    await userEvent.clear(typed);
    await userEvent.type(typed, '1.16.4');
    await userEvent.click(
      screen.getByRole('button', { name: 'Save settings' })
    );

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(
      'ws-01J000000000000000000000',
      expect.objectContaining({ engine_version: '1.16.4' })
    );
  });

  it('hides the checklist and reports ready once every step is done', async () => {
    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });

    expect(await screen.findByTestId('workspace-status')).toHaveTextContent(
      'Ready'
    );
    expect(screen.queryByTestId('setup-checklist')).not.toBeInTheDocument();
  });

  it('shows a sensitive variable as write only rather than its value', async () => {
    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });
    await userEvent.click(railLink('Variables'));

    expect(
      await screen.findByText('Sensitive, write only')
    ).toBeInTheDocument();
    expect(screen.getByText('us-west-2')).toBeInTheDocument();
  });

  it('offers the presigned upload form on the configuration versions page', async () => {
    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });
    await userEvent.click(railLink('Configuration versions'));

    expect(
      await screen.findByRole('form', {
        name: 'Upload a configuration version',
      })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Plan and apply' })
    ).toBeInTheDocument();
  });

  it.each(['terraform', 'env'] as const)(
    'preserves HCL only when saving a terraform variable as %s',
    async (category) => {
      const variable = aVariable({ hcl: true, value: '["west", "east"]' });
      apiMock.listVariables.mockResolvedValue({ items: [variable] });
      apiMock.putVariable.mockResolvedValue(variable);
      renderDetail('/workspaces/ws-01J000000000000000000000/variables');

      await userEvent.click(
        await screen.findByRole('button', { name: 'Edit' })
      );
      await userEvent.selectOptions(
        screen.getByLabelText('Category'),
        category
      );
      await userEvent.click(
        screen.getByRole('button', { name: 'Save variable' })
      );

      expect(apiMock.putVariable).toHaveBeenCalledWith(
        variable.workspace_id,
        variable.key,
        expect.objectContaining({
          value: variable.value,
          category,
          hcl: category === 'terraform',
        })
      );
    }
  );

  it('declares the file size and PUTs with every signed header', async () => {
    const put = vi.fn<typeof globalThis.fetch>(() =>
      Promise.resolve(new Response(null, { status: 200 }))
    );
    vi.stubGlobal('fetch', put);
    const headers = {
      'Content-Type': 'application/gzip',
      'Content-Length': '7',
    };
    apiMock.createConfigVersion.mockResolvedValue({
      config_version: aConfigVersion({ status: 'pending' }),
      upload_url: 'https://bucket.s3.test/x',
      headers,
      expires_in: 900,
    });

    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });
    await userEvent.click(railLink('Configuration versions'));
    await screen.findByRole('form', {
      name: 'Upload a configuration version',
    });
    const file = new File(['tarball'], 'config.tar.gz', {
      type: 'application/gzip',
    });

    await userEvent.upload(
      screen.getByLabelText('Configuration tarball'),
      file
    );
    const listReads = apiMock.listConfigVersions.mock.calls.length;
    fireEvent.submit(
      screen.getByRole('form', { name: 'Upload a configuration version' })
    );

    await screen.findByText('Uploaded config.tar.gz.');

    expect(apiMock.createConfigVersion).toHaveBeenCalledWith(
      'ws-01J000000000000000000000',
      { size_bytes: file.size },
      expect.objectContaining({ signal: expect.any(AbortSignal) as unknown })
    );
    await waitFor(() => {
      expect(apiMock.listConfigVersions.mock.calls.length).toBeGreaterThan(
        listReads
      );
    });
    expect(put.mock.calls[0]?.[1]?.headers).toEqual(headers);
    vi.unstubAllGlobals();
  });

  it('lists the workspace runs behind the filter tabs on the runs page', async () => {
    renderDetail('/workspaces/ws-01J000000000000000000000/runs');

    await screen.findByRole('heading', { name: 'platform' });

    expect(
      await screen.findByRole('heading', { name: 'Run list' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('tab', { name: /Needs attention/ })
    ).toHaveTextContent('1');
    expect(screen.getAllByTestId('run-state-badge')[0]).toHaveAttribute(
      'data-state',
      'planned'
    );
    expect(apiMock.listRuns).toHaveBeenCalledWith(
      { workspace_id: 'ws-01J000000000000000000000' },
      expect.anything()
    );
  });
});

/** The 409 the API answers a run start with when the workspace has no role. */
function runRoleMissingError(): ApiError {
  return new ApiError({
    status: 409,
    statusText: 'Conflict',
    url: 'https://api.test/api/v1/runs',
    method: 'POST',
    body: {
      success: false,
      status: 409,
      message: 'The workspace has no run role.',
      request_id: 'r-1',
      error_code: 'RUN_ROLE_MISSING',
    },
  });
}

describe('WorkspaceLayout setup checklist', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aFreshWorkspace());
    apiMock.readRunRoleCheck.mockResolvedValue(UNVERIFIED);
    apiMock.listVariables.mockResolvedValue({ items: [] });
    apiMock.listConfigVersions.mockResolvedValue({ items: [] });
    apiMock.listRuns.mockResolvedValue({ items: [] });
  });

  it('leads with the connect step current and the rest blocked', async () => {
    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');

    expect(screen.getByTestId('workspace-status')).toHaveTextContent(
      'Setup incomplete'
    );
    expect(within(checklist).getByTestId('setup-step-connect')).toHaveAttribute(
      'data-status',
      'current'
    );
    expect(within(checklist).getByTestId('setup-step-upload')).toHaveAttribute(
      'data-status',
      'blocked'
    );
    expect(within(checklist).getByTestId('setup-step-plan')).toHaveAttribute(
      'data-status',
      'blocked'
    );
    expect(
      within(checklist).getByRole('form', { name: 'Run role' })
    ).toBeInTheDocument();
  });

  it('fills every snippet with the external id and both runner principals', async () => {
    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');
    const setup = aFreshWorkspace().run_role_setup;

    for (const label of [
      'Terraform',
      'CloudFormation',
      'AWS CLI',
      'Trust policy',
    ]) {
      await userEvent.click(
        within(checklist).getByRole('tab', { name: label })
      );
      const code = within(checklist).getByTestId('code-block');
      expect(code).toHaveTextContent(setup.external_id);
      for (const arn of setup.principal_arns) {
        expect(code).toHaveTextContent(arn);
      }
    }
    expect(
      within(checklist).getByRole('tab', { name: 'Trust policy' })
    ).toHaveAttribute('aria-selected', 'true');
  });

  it('names the prefix the runner may assume beside the ARN input', async () => {
    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');

    expect(
      within(checklist).getByLabelText('Role ARN')
    ).toHaveAccessibleDescription(
      new RegExp(`starts with ${assumablePrefix()}`)
    );
  });

  it('saves the role ARN through a patch carrying only that field', async () => {
    const arn = aWorkspace().run_role_arn ?? '';
    apiMock.updateWorkspace.mockResolvedValue(
      aFreshWorkspace({ run_role_arn: arn })
    );

    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');
    await userEvent.type(within(checklist).getByLabelText('Role ARN'), arn);
    await userEvent.click(
      within(checklist).getByRole('button', { name: 'Save' })
    );

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(
      'ws-01J000000000000000000000',
      { run_role_arn: arn }
    );
    expect(await within(checklist).findByRole('status')).toHaveTextContent(
      'Saved.'
    );
  });

  it('refuses a role ARN outside the assumable prefix without a request', async () => {
    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');
    await userEvent.type(
      within(checklist).getByLabelText('Role ARN'),
      'arn:aws:iam::123456789012:role/terraform-run'
    );
    await userEvent.click(
      within(checklist).getByRole('button', { name: 'Save' })
    );

    expect(await within(checklist).findByRole('alert')).toHaveTextContent(
      `The role name must start with ${assumablePrefix()}`
    );
    expect(apiMock.updateWorkspace).not.toHaveBeenCalled();
  });

  it('reads the runner record on the settings page without writing', async () => {
    apiMock.getWorkspace.mockResolvedValue(
      aFreshWorkspace({ run_role_arn: aWorkspace().run_role_arn })
    );

    renderDetail(RUN_ROLE_SETTINGS);

    const status = await screen.findByTestId('run-role-status');
    expect(apiMock.readRunRoleCheck).toHaveBeenCalledWith(
      'ws-01J000000000000000000000',
      expect.objectContaining({ signal: expect.any(AbortSignal) as unknown })
    );
    expect(apiMock.checkRunRole).not.toHaveBeenCalled();
    expect(status).toHaveAttribute('data-connection', 'unverified');
    expect(status).toHaveTextContent('No run has assumed this role yet.');
  });

  it('reports a run that assumed the role with the account id', async () => {
    apiMock.getWorkspace.mockResolvedValue(
      aFreshWorkspace({ run_role_arn: aWorkspace().run_role_arn })
    );
    apiMock.checkRunRole.mockResolvedValue({
      connected: true,
      status: 'connected',
      account_id: '123456789012',
      error: null,
      run_id: 'run-1',
      checked_at: '2026-09-17T00:05:00Z',
    });

    renderDetail(RUN_ROLE_SETTINGS);

    const checkButton = await screen.findByRole('button', {
      name: 'Check connection',
    });
    const readsBefore = apiMock.getWorkspace.mock.calls.length;
    apiMock.readRunRoleCheck.mockResolvedValue(CONNECTED);
    await userEvent.click(checkButton);

    expect(apiMock.checkRunRole).toHaveBeenCalledWith(
      'ws-01J000000000000000000000'
    );
    await waitFor(() => {
      expect(screen.getByTestId('run-role-status')).toHaveAttribute(
        'data-connection',
        'connected'
      );
    });
    await waitFor(() => {
      expect(apiMock.getWorkspace.mock.calls.length).toBeGreaterThan(
        readsBefore
      );
    });
    expect(screen.getByTestId('run-role-status')).toHaveTextContent(
      'The runner assumed this role in account 123456789012'
    );
  });

  it('reports a run the role refused with what to fix', async () => {
    apiMock.getWorkspace.mockResolvedValue(
      aFreshWorkspace({ run_role_arn: aWorkspace().run_role_arn })
    );
    apiMock.readRunRoleCheck.mockResolvedValue({
      connected: false,
      status: 'failed',
      account_id: null,
      error: 'Its trust policy has to name every runner task role.',
      run_id: 'run-1',
      checked_at: null,
    });

    renderDetail(RUN_ROLE_SETTINGS);

    await waitFor(() => {
      expect(screen.getByTestId('run-role-status')).toHaveAttribute(
        'data-connection',
        'failed'
      );
    });
    expect(screen.getByTestId('run-role-status')).toHaveTextContent(
      'The runner could not assume the role. Its trust policy has to name every runner task role.'
    );
  });

  it('lets a saved but unverified role start a run, which is the check', async () => {
    apiMock.getWorkspace.mockResolvedValue(
      aFreshWorkspace({ run_role_arn: aWorkspace().run_role_arn })
    );
    apiMock.listConfigVersions.mockResolvedValue({
      items: [aConfigVersion()],
    });

    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');
    expect(
      within(checklist).getByRole('button', { name: 'Run a plan' })
    ).toBeEnabled();
    for (const button of screen.getAllByRole('button', { name: '+ New run' })) {
      expect(button).toBeEnabled();
    }
    expect(screen.getAllByText('Not verified').length).toBeGreaterThan(0);
  });

  it('disables the check until an ARN is saved', async () => {
    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');

    expect(
      within(checklist).getByRole('button', { name: 'Check connection' })
    ).toBeDisabled();
  });

  it('disables run starts with the reason while no account is connected', async () => {
    apiMock.listConfigVersions.mockResolvedValue({
      items: [aConfigVersion()],
    });

    renderDetail();

    await screen.findByTestId('setup-checklist');
    await userEvent.click(railLink('Configuration versions'));

    const planOnly = await screen.findByRole('button', { name: 'Plan only' });
    expect(planOnly).toBeDisabled();
    expect(planOnly).toHaveAttribute(
      'title',
      'Connect an AWS account before starting a run.'
    );
    expect(
      screen.getByRole('button', { name: 'Plan and apply' })
    ).toBeDisabled();
    expect(screen.getByRole('note')).toHaveTextContent(
      'Connect an AWS account before starting a run.'
    );
  });

  it('renders a RUN_ROLE_MISSING refusal as the same sentence', async () => {
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.listConfigVersions.mockResolvedValue({
      items: [aConfigVersion()],
    });
    apiMock.createRun.mockRejectedValue(runRoleMissingError());

    renderDetail();

    await screen.findByTestId('setup-checklist');
    await userEvent.click(screen.getByRole('button', { name: 'Run a plan' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Connect an AWS account before starting a run.'
    );
  });

  it('moves the checklist along as the account, upload and plan land', async () => {
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.listConfigVersions.mockResolvedValue({
      items: [aConfigVersion()],
    });
    apiMock.createRun.mockResolvedValue(aRun('pending'));

    apiMock.getRun.mockResolvedValue(aRun('pending'));
    apiMock.getRunLogs.mockResolvedValue({
      run_id: 'run-01J000000000000000000000',
      phase: 'plan',
      events: [],
      next_after: null,
    });

    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');
    expect(within(checklist).getByTestId('setup-step-connect')).toHaveAttribute(
      'data-status',
      'done'
    );
    expect(within(checklist).getByTestId('setup-step-upload')).toHaveAttribute(
      'data-status',
      'done'
    );
    expect(within(checklist).getByTestId('setup-step-plan')).toHaveAttribute(
      'data-status',
      'current'
    );

    await userEvent.click(screen.getByRole('button', { name: 'Run a plan' }));

    expect(apiMock.createRun).toHaveBeenCalledWith({
      workspace_id: 'ws-01J000000000000000000000',
      config_version_id: 'cv-01J000000000000000000000',
      plan_only: true,
    });
    expect(
      await screen.findByRole('heading', { name: 'Seed the stack.' })
    ).toBeInTheDocument();
  });

  it('offers the upload form inside the checklist once connected', async () => {
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());

    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');

    expect(within(checklist).getByTestId('setup-step-upload')).toHaveAttribute(
      'data-status',
      'current'
    );
    expect(
      within(checklist).getByRole('form', { name: 'Upload a configuration' })
    ).toBeInTheDocument();
  });

  it('opens any checklist step, not only the current one', async () => {
    apiMock.getWorkspace.mockResolvedValue(aFreshWorkspace());
    apiMock.listConfigVersions.mockResolvedValue({ items: [] });
    apiMock.listRuns.mockResolvedValue({ items: [] });

    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');
    const upload = within(checklist).getByTestId('setup-step-upload');
    expect(upload).toHaveAttribute('data-status', 'blocked');
    const toggle = within(upload).getByRole('button', {
      name: /^Upload a configuration/,
    });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    await userEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(
      within(upload).getByRole('form', { name: 'Upload a configuration' })
    ).toBeInTheDocument();

    await userEvent.click(toggle);

    expect(
      within(upload).queryByRole('form', { name: 'Upload a configuration' })
    ).not.toBeInTheDocument();
  });

  it('counts the account step done once a run planned through the saved role', async () => {
    apiMock.getWorkspace.mockResolvedValue(
      aWorkspace({ run_role_account_id: null, run_role_checked_at: null })
    );
    apiMock.listConfigVersions.mockResolvedValue({
      items: [aConfigVersion()],
    });
    apiMock.listRuns.mockResolvedValue({ items: [aRun('applied')] });

    renderDetail();

    await waitFor(() => {
      expect(screen.getByTestId('workspace-status')).toHaveTextContent('Ready');
    });
    expect(screen.queryByTestId('setup-checklist')).not.toBeInTheDocument();
  });
});

describe('WorkspaceLayout account status', () => {
  const unstamped = (): ReturnType<typeof aWorkspace> =>
    aWorkspace({ run_role_account_id: null, run_role_checked_at: null });

  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(unstamped());
    apiMock.listConfigVersions.mockResolvedValue({
      items: [aConfigVersion()],
    });
    apiMock.listRuns.mockResolvedValue({ items: [aRun('planned')] });
  });

  it('shows the account the check found in the header and overview, even before a check is stamped', async () => {
    apiMock.readRunRoleCheck.mockResolvedValue(CONNECTED);

    renderDetail();

    await waitFor(() => {
      expect(screen.getByTestId('workspace-account')).toHaveTextContent(
        '123456789012'
      );
    });
    expect(screen.getByTestId('workspace-account')).toHaveAttribute(
      'data-connection',
      'connected'
    );
    expect(screen.getByTestId('overview-account')).toHaveTextContent(
      '123456789012'
    );
    expect(apiMock.checkRunRole).not.toHaveBeenCalled();
  });

  it('shows the connected repository and branch in the overview', async () => {
    apiMock.readRunRoleCheck.mockResolvedValue(CONNECTED);
    apiMock.getWorkspace.mockResolvedValue(
      aWorkspace({ vcs_repo: 'WebbPulse/infra', tracked_branch: 'staging' })
    );

    renderDetail();

    await waitFor(() => {
      expect(screen.getByTestId('overview-repository')).toHaveTextContent(
        'WebbPulse/infra on staging'
      );
    });
  });

  it('says when no repository is connected', async () => {
    apiMock.readRunRoleCheck.mockResolvedValue(CONNECTED);

    renderDetail();

    await waitFor(() => {
      expect(screen.getByTestId('overview-repository')).toHaveTextContent(
        'Not connected'
      );
    });
  });

  it('agrees with the connection panel on the settings page', async () => {
    apiMock.readRunRoleCheck.mockResolvedValue(CONNECTED);

    renderDetail(RUN_ROLE_SETTINGS);

    await waitFor(() => {
      expect(screen.getByTestId('run-role-status')).toHaveAttribute(
        'data-connection',
        'connected'
      );
    });
    expect(screen.getByTestId('workspace-account')).toHaveAttribute(
      'data-connection',
      'connected'
    );
    expect(screen.getByTestId('workspace-account')).toHaveTextContent(
      '123456789012'
    );
    expect(apiMock.readRunRoleCheck).toHaveBeenCalledTimes(1);
  });

  it('says a refused role failed rather than leaving it unverified', async () => {
    apiMock.readRunRoleCheck.mockResolvedValue({
      ...UNVERIFIED,
      status: 'failed' as const,
      error: 'Its trust policy has to name every runner task role.',
    });

    renderDetail();

    await waitFor(() => {
      expect(screen.getByTestId('workspace-account')).toHaveTextContent(
        'Connection failed'
      );
    });
    expect(screen.getByTestId('overview-account')).toHaveTextContent(
      'Connection failed'
    );
  });

  it('reads the check again when a run finishes', async () => {
    apiMock.listRuns.mockResolvedValue({ items: [aRun('planning')] });
    apiMock.readRunRoleCheck.mockResolvedValue(UNVERIFIED);

    renderDetail();

    await waitFor(() => {
      expect(screen.getByTestId('workspace-account')).toHaveTextContent(
        'Not verified'
      );
    });
    const readsBefore = apiMock.readRunRoleCheck.mock.calls.length;

    apiMock.listRuns.mockResolvedValue({ items: [aRun('planned')] });
    apiMock.readRunRoleCheck.mockResolvedValue(CONNECTED);
    act(() => {
      invalidateQueries('runs:ws-01J000000000000000000000');
    });

    await waitFor(() => {
      expect(screen.getByTestId('workspace-account')).toHaveTextContent(
        '123456789012'
      );
    });
    expect(apiMock.readRunRoleCheck.mock.calls.length).toBeGreaterThan(
      readsBefore
    );
  });

  it('does not read the check again while the runs have not changed', async () => {
    apiMock.readRunRoleCheck.mockResolvedValue(UNVERIFIED);

    renderDetail();

    await waitFor(() => {
      expect(screen.getByTestId('workspace-account')).toHaveTextContent(
        'Not verified'
      );
    });
    const readsBefore = apiMock.readRunRoleCheck.mock.calls.length;
    const runReads = apiMock.listRuns.mock.calls.length;

    act(() => {
      invalidateQueries('runs:ws-01J000000000000000000000');
    });

    await waitFor(() => {
      expect(apiMock.listRuns.mock.calls.length).toBeGreaterThan(runReads);
    });
    expect(apiMock.readRunRoleCheck.mock.calls.length).toBe(readsBefore);
  });

  it('refreshes the header when the panel checks the connection', async () => {
    apiMock.readRunRoleCheck.mockResolvedValue(UNVERIFIED);
    apiMock.checkRunRole.mockResolvedValue(CONNECTED);

    renderDetail(RUN_ROLE_SETTINGS);

    const checkButton = await screen.findByRole('button', {
      name: 'Check connection',
    });
    await waitFor(() => {
      expect(screen.getByTestId('workspace-account')).toHaveTextContent(
        'Not verified'
      );
    });
    apiMock.readRunRoleCheck.mockResolvedValue(CONNECTED);
    await userEvent.click(checkButton);

    await waitFor(() => {
      expect(screen.getByTestId('workspace-account')).toHaveTextContent(
        '123456789012'
      );
    });
    expect(screen.getByTestId('run-role-status')).toHaveAttribute(
      'data-connection',
      'connected'
    );
  });

  it('falls back to the stamped account while the check has not answered', async () => {
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.readRunRoleCheck.mockReturnValue(new Promise(() => undefined));

    renderDetail();

    expect(await screen.findByTestId('workspace-account')).toHaveTextContent(
      '123456789012'
    );
  });
});

/** What quick setup answers for the fixture workspace. */
function aQuickSetup(permissions: string | null = null): {
  account_id: string;
  role_arn: string;
  role_name: string;
  stack_name: string;
  region: string;
  permissions_policy_arn: string | null;
  expires_in: number;
  console_url: string;
} {
  const roleName = aWorkspace().run_role_setup.role_name;
  return {
    account_id: '123456789012',
    role_arn: `arn:aws:iam::123456789012:role/${roleName}`,
    role_name: roleName,
    stack_name: roleName,
    region: 'us-west-2',
    permissions_policy_arn: permissions,
    expires_in: 3600,
    console_url:
      'https://us-west-2.console.aws.amazon.com/cloudformation/home?region=us-west-2#/stacks/quickcreate',
  };
}

/** A stand in for the tab `window.open` returns. */
function aTab(): { opener: unknown; location: { href: string }; close: Mock } {
  return { opener: {}, location: { href: '' }, close: vi.fn() };
}

describe('AWS quick setup', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aFreshWorkspace());
    apiMock.readRunRoleCheck.mockResolvedValue(UNVERIFIED);
    apiMock.listVariables.mockResolvedValue({ items: [] });
    apiMock.listConfigVersions.mockResolvedValue({ items: [] });
    apiMock.listRuns.mockResolvedValue({ items: [] });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('opens AWS CloudFormation in a new tab with nothing to copy back', async () => {
    const tab = aTab();
    const open = vi.spyOn(window, 'open').mockReturnValue(tab as never);
    apiMock.startRunRoleQuickSetup.mockResolvedValue(
      aQuickSetup('arn:aws:iam::aws:policy/ReadOnlyAccess')
    );

    renderDetail();

    const setup = await screen.findByRole('form', { name: 'AWS quick setup' });
    await userEvent.type(
      within(setup).getByLabelText('AWS account ID'),
      '1234-5678-9012'
    );
    await userEvent.selectOptions(
      within(setup).getByLabelText('Permissions policy'),
      'read_only'
    );
    await userEvent.click(
      within(setup).getByRole('button', { name: 'Open AWS CloudFormation' })
    );

    expect(open).toHaveBeenCalledWith('', '_blank');
    expect(apiMock.startRunRoleQuickSetup).toHaveBeenCalledWith(
      'ws-01J000000000000000000000',
      { account_id: '123456789012', permissions: 'read_only' }
    );
    const steps = await screen.findByTestId('quick-setup-steps');
    expect(steps).toHaveTextContent(aQuickSetup().role_arn);
    expect(steps).toHaveTextContent('Create stack');
    expect(tab.opener).toBeNull();
    expect(tab.location.href).toBe(aQuickSetup().console_url);
    expect(
      screen.getByRole('button', { name: 'Open AWS CloudFormation again' })
    ).toBeInTheDocument();
  });

  it('offers the link when the browser blocks the new tab', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    apiMock.startRunRoleQuickSetup.mockResolvedValue(aQuickSetup());

    renderDetail();

    const setup = await screen.findByRole('form', { name: 'AWS quick setup' });
    await userEvent.type(
      within(setup).getByLabelText('AWS account ID'),
      '123456789012'
    );
    await userEvent.click(
      within(setup).getByRole('button', { name: 'Open AWS CloudFormation' })
    );

    const steps = await screen.findByTestId('quick-setup-steps');
    expect(
      within(steps).getByRole('link', { name: 'Open AWS CloudFormation' })
    ).toHaveAttribute('href', aQuickSetup().console_url);
  });

  it('closes the blank tab and says why when the request fails', async () => {
    const tab = aTab();
    vi.spyOn(window, 'open').mockReturnValue(tab as never);
    apiMock.startRunRoleQuickSetup.mockRejectedValue(
      new ApiError({
        status: 503,
        statusText: 'Service Unavailable',
        url: 'https://api.test/api/v1/workspaces/ws-01J000000000000000000000/run-role/quick-setup',
        method: 'POST',
        body: {
          success: false,
          status: 503,
          message: 'AWS quick setup is not available in this environment.',
          request_id: 'r-1',
        },
      })
    );

    renderDetail();

    const setup = await screen.findByRole('form', { name: 'AWS quick setup' });
    await userEvent.type(
      within(setup).getByLabelText('AWS account ID'),
      '123456789012'
    );
    await userEvent.click(
      within(setup).getByRole('button', { name: 'Open AWS CloudFormation' })
    );

    await waitFor(() => {
      expect(tab.close).toHaveBeenCalled();
    });
    expect(await within(setup).findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByTestId('quick-setup-steps')).not.toBeInTheDocument();
  });

  it('refuses a malformed account id without a request or a tab', async () => {
    const open = vi.spyOn(window, 'open');

    renderDetail();

    const setup = await screen.findByRole('form', { name: 'AWS quick setup' });
    await userEvent.type(
      within(setup).getByLabelText('AWS account ID'),
      '12345'
    );
    await userEvent.click(
      within(setup).getByRole('button', { name: 'Open AWS CloudFormation' })
    );

    expect(await within(setup).findByRole('alert')).toHaveTextContent(
      'Enter the 12 digit AWS account ID.'
    );
    expect(open).not.toHaveBeenCalled();
    expect(apiMock.startRunRoleQuickSetup).not.toHaveBeenCalled();
  });

  it('prefills the account from the saved role on the settings page', async () => {
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());

    renderDetail(RUN_ROLE_SETTINGS);

    const setup = await screen.findByRole('form', { name: 'AWS quick setup' });
    expect(within(setup).getByLabelText('AWS account ID')).toHaveValue(
      '123456789012'
    );
  });

  it('keeps the manual path behind a disclosure', async () => {
    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');
    const manual = within(checklist)
      .getByText('Set up the role manually')
      .closest('details');
    expect(manual).not.toBeNull();
    expect(manual).not.toHaveAttribute('open');
    expect(
      within(manual as HTMLElement).getByRole('form', { name: 'Run role' })
    ).toBeInTheDocument();
  });
});
