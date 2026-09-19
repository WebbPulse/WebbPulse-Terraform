import { fireEvent, screen, within } from '@testing-library/react';
import { ApiError } from '@webbpulse/api-client';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import {
  aConfigVersion,
  aFreshWorkspace,
  aRun,
  aVariable,
  aWorkspace,
} from '../test-helpers/fixtures';
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

const { WorkspaceDetail } = await import('./WorkspaceDetail');

/** Mounts the detail page on a route carrying the workspace id. */
function renderDetail(): void {
  renderWithAuth(
    <Routes>
      <Route path="/workspaces/:workspaceId" element={<WorkspaceDetail />} />
    </Routes>,
    signedInAuthClient(),
    ['/workspaces/ws-01J000000000000000000000']
  );
}

describe('WorkspaceDetail', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
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

  it('opens on the overview tab with the workspace settings', async () => {
    renderDetail();

    expect(
      await screen.findByRole('heading', { name: 'platform' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('form', { name: 'Workspace settings' })
    ).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
  });

  it('shows all four tabs', async () => {
    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });
    for (const label of [
      'Overview',
      'Variables',
      'Configuration versions',
      'Runs',
    ]) {
      expect(screen.getByRole('tab', { name: label })).toBeInTheDocument();
    }
  });

  it('patches the editable fields without the name or the run role', async () => {
    apiMock.updateWorkspace.mockResolvedValue(aWorkspace());

    renderDetail();

    await screen.findByRole('form', { name: 'Workspace settings' });
    const version = screen.getByLabelText('Engine version');
    await userEvent.clear(version);
    await userEvent.type(version, '1.12.0');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(
      'ws-01J000000000000000000000',
      {
        description: 'The platform workspace.',
        engine: 'terraform',
        engine_version: '1.12.0',
        working_directory: 'terraform',
      }
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
    await userEvent.click(screen.getByRole('tab', { name: 'Variables' }));

    expect(await screen.findByText('Write only')).toBeInTheDocument();
    expect(screen.getByText('us-west-2')).toBeInTheDocument();
  });

  it('offers the presigned upload form on the configuration versions tab', async () => {
    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });
    await userEvent.click(
      screen.getByRole('tab', { name: 'Configuration versions' })
    );

    expect(
      await screen.findByRole('form', {
        name: 'Upload a configuration version',
      })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Plan and apply' })
    ).toBeInTheDocument();
  });

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
    await userEvent.click(
      screen.getByRole('tab', { name: 'Configuration versions' })
    );
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
    fireEvent.submit(
      screen.getByRole('form', { name: 'Upload a configuration version' })
    );

    await screen.findByText('Uploaded.');

    expect(apiMock.createConfigVersion).toHaveBeenCalledWith(
      'ws-01J000000000000000000000',
      { size_bytes: file.size }
    );
    expect(put.mock.calls[0]?.[1]?.headers).toEqual(headers);
    vi.unstubAllGlobals();
  });

  it('lists the workspace runs on the runs tab', async () => {
    renderDetail();

    await screen.findByRole('heading', { name: 'platform' });
    await userEvent.click(screen.getByRole('tab', { name: 'Runs' }));

    expect(await screen.findByTestId('run-state-badge')).toHaveAttribute(
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

describe('WorkspaceDetail setup checklist', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aFreshWorkspace());
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
      /starts with webbpulse-terraform-staging-workspace-/
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
      'The role name must start with webbpulse-terraform-staging-workspace-'
    );
    expect(apiMock.updateWorkspace).not.toHaveBeenCalled();
  });

  it('reports a passing connection check with the account id', async () => {
    apiMock.getWorkspace.mockResolvedValue(
      aFreshWorkspace({ run_role_arn: aWorkspace().run_role_arn })
    );
    apiMock.checkRunRole.mockResolvedValue({
      connected: true,
      account_id: '123456789012',
      error: null,
    });

    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');
    await userEvent.click(
      within(checklist).getByRole('button', { name: 'Check connection' })
    );

    expect(apiMock.checkRunRole).toHaveBeenCalledWith(
      'ws-01J000000000000000000000'
    );
    const status = await within(checklist).findByTestId('run-role-status');
    expect(status).toHaveAttribute('data-connection', 'connected');
    expect(status).toHaveTextContent('Connected to account 123456789012');
  });

  it('reports a failing connection check with its reason', async () => {
    apiMock.getWorkspace.mockResolvedValue(
      aFreshWorkspace({ run_role_arn: aWorkspace().run_role_arn })
    );
    apiMock.checkRunRole.mockResolvedValue({
      connected: false,
      account_id: null,
      error: 'The role refused the runner. Check the trust policy.',
    });

    renderDetail();

    const checklist = await screen.findByTestId('setup-checklist');
    await userEvent.click(
      within(checklist).getByRole('button', { name: 'Check connection' })
    );

    const status = await within(checklist).findByTestId('run-role-status');
    expect(status).toHaveAttribute('data-connection', 'failed');
    expect(status).toHaveTextContent(
      'The role refused the runner. Check the trust policy.'
    );
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
    await userEvent.click(
      screen.getByRole('tab', { name: 'Configuration versions' })
    );

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

    renderWithAuth(
      <Routes>
        <Route path="/workspaces/:workspaceId" element={<WorkspaceDetail />} />
        <Route path="/runs/:runId" element={<p>Run page</p>} />
      </Routes>,
      signedInAuthClient(),
      ['/workspaces/ws-01J000000000000000000000']
    );

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
    expect(await screen.findByText('Run page')).toBeInTheDocument();
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
});
