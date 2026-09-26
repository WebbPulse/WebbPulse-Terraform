import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { Layout } from '../../../components/Layout';
import {
  aConfigVersion,
  aRun,
  aWorkspace,
} from '../../../test-helpers/fixtures';
import {
  renderWithAuth,
  signedInAuthClient,
} from '../../../test-helpers/renderWithAuth';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../../../test-helpers/apiMock';
import { latestUploaded } from './latestUploaded';

vi.mock('../../../api/client', () => apiClientModuleMock());

const { workspaceRoutes } = await import('../../workspaceRoutes');

/** Mounts the destruction and deletion settings page inside the shell. */
function renderDeletion(): void {
  renderWithAuth(
    <Routes>
      <Route element={<Layout />}>{workspaceRoutes()}</Route>
    </Routes>,
    signedInAuthClient(),
    ['/workspaces/ws-01J000000000000000000000/settings/deletion']
  );
}

/** Opens the destroy dialog from the settings page. */
async function openDestroyDialog(): Promise<void> {
  await userEvent.click(
    await screen.findByRole('button', { name: 'Queue destroy plan' })
  );
  await screen.findByRole('form', { name: 'Queue a destroy plan' });
}

/** The submit button inside the destroy dialog. */
function submitButton(): HTMLElement {
  const form = screen.getByRole('form', { name: 'Queue a destroy plan' });
  const button = form.querySelector('button[type="submit"]');
  if (!(button instanceof HTMLElement)) {
    throw new Error('No submit button in the destroy form');
  }
  return button;
}

describe('DestroyPlanSection', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.listConfigVersions.mockResolvedValue({
      items: [
        aConfigVersion({
          config_version_id: 'cv-old',
          created_at: '2026-09-16T00:00:00Z',
        }),
        aConfigVersion(),
        aConfigVersion({
          config_version_id: 'cv-pending',
          status: 'pending',
          created_at: '2026-09-18T00:00:00Z',
        }),
      ],
    });
    apiMock.listRuns.mockResolvedValue({ items: [aRun('applied')] });
  });

  it('holds the destroy until the workspace name is typed back', async () => {
    renderDeletion();
    await openDestroyDialog();

    expect(submitButton()).toBeDisabled();
    await userEvent.type(
      screen.getByLabelText('Type platform to confirm'),
      'platfor'
    );
    expect(submitButton()).toBeDisabled();
    expect(apiMock.createRun).not.toHaveBeenCalled();
  });

  it('queues a destroy run on the newest upload and opens it', async () => {
    apiMock.createRun.mockResolvedValue(
      aRun('pending', { run_id: 'run-destroy', is_destroy: true })
    );
    apiMock.getRun.mockResolvedValue(
      aRun('planning', { run_id: 'run-destroy', is_destroy: true })
    );

    renderDeletion();
    await openDestroyDialog();
    await userEvent.type(
      screen.getByLabelText('Type platform to confirm'),
      'platform'
    );
    await userEvent.click(submitButton());

    expect(apiMock.createRun).toHaveBeenCalledWith({
      workspace_id: 'ws-01J000000000000000000000',
      config_version_id: 'cv-01J000000000000000000000',
      plan_only: false,
      is_destroy: true,
      message: 'Destroy plan queued from settings',
    });
    expect(await screen.findByTestId('destroy-badge')).toBeInTheDocument();
  });

  it('can queue a plan only destroy run', async () => {
    apiMock.createRun.mockResolvedValue(
      aRun('pending', {
        run_id: 'run-destroy',
        is_destroy: true,
        plan_only: true,
      })
    );
    apiMock.getRun.mockResolvedValue(
      aRun('planning', {
        run_id: 'run-destroy',
        is_destroy: true,
        plan_only: true,
      })
    );

    renderDeletion();
    await openDestroyDialog();
    await userEvent.click(
      screen.getByRole('checkbox', { name: 'Plan only, never apply' })
    );
    await userEvent.type(
      screen.getByLabelText('Type platform to confirm'),
      'platform'
    );
    await userEvent.click(submitButton());

    expect(apiMock.createRun).toHaveBeenCalledWith(
      expect.objectContaining({ plan_only: true, is_destroy: true })
    );
  });

  it('is unavailable until a configuration version is uploaded', async () => {
    apiMock.listConfigVersions.mockResolvedValue({
      items: [aConfigVersion({ status: 'pending' })],
    });

    renderDeletion();

    expect(
      await screen.findByText(
        'Upload a configuration version before queueing a destroy plan.'
      )
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Queue destroy plan' })
    ).toBeDisabled();
  });

  it('picks the newest uploaded version', () => {
    expect(
      latestUploaded([
        aConfigVersion({
          config_version_id: 'a',
          created_at: '2026-09-01T00:00:00Z',
        }),
        aConfigVersion({
          config_version_id: 'b',
          created_at: '2026-09-03T00:00:00Z',
        }),
        aConfigVersion({
          config_version_id: 'c',
          status: 'pending',
          created_at: '2026-09-04T00:00:00Z',
        }),
      ])?.config_version_id
    ).toBe('b');
    expect(latestUploaded([])).toBeNull();
  });
});
