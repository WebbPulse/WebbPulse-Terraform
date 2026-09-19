import { fireEvent, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import {
  aConfigVersion,
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

  it('patches the editable fields, with the run role and without the name', async () => {
    apiMock.updateWorkspace.mockResolvedValue(aWorkspace());

    renderDetail();

    await screen.findByRole('form', { name: 'Workspace settings' });
    const arn = screen.getByLabelText('Run role ARN');
    await userEvent.clear(arn);
    await userEvent.type(arn, 'arn:aws:iam::123456789012:role/other-run');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(apiMock.updateWorkspace).toHaveBeenCalledWith(
      'ws-01J000000000000000000000',
      {
        description: 'The platform workspace.',
        engine: 'terraform',
        engine_version: '1.11.0',
        working_directory: 'terraform',
        run_role_arn: 'arn:aws:iam::123456789012:role/other-run',
      }
    );
  });

  it('refuses to patch a run role ARN that is not one', async () => {
    renderDetail();

    await screen.findByRole('form', { name: 'Workspace settings' });
    const arn = screen.getByLabelText('Run role ARN');
    await userEvent.clear(arn);
    await userEvent.type(arn, 'arn:aws:iam::12:role/short');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Enter a role ARN like arn:aws:iam::123456789012:role/terraform-run.'
    );
    expect(apiMock.updateWorkspace).not.toHaveBeenCalled();
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
