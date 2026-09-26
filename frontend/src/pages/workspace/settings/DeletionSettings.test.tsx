import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from '@webbpulse/api-client';
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

vi.mock('../../../api/client', () => apiClientModuleMock());

const { workspaceRoutes } = await import('../../workspaceRoutes');

const WORKSPACE_ID = 'ws-01J000000000000000000000';

/** Mounts the destruction and deletion settings page inside the shell. */
function renderDeletion(): void {
  renderWithAuth(
    <Routes>
      <Route element={<Layout />}>{workspaceRoutes()}</Route>
      <Route path="/workspaces" element={<p>Workspaces list</p>} />
    </Routes>,
    signedInAuthClient(),
    [`/workspaces/${WORKSPACE_ID}/settings/deletion`]
  );
}

/** A 409 from the delete route carrying the given code and message. */
function conflict(errorCode: string, message: string): ApiError {
  return new ApiError({
    status: 409,
    statusText: 'Conflict',
    url: `https://api.test/api/v1/workspaces/${WORKSPACE_ID}`,
    method: 'DELETE',
    body: {
      success: false,
      status: 409,
      message,
      request_id: 'r-1',
      error_code: errorCode,
    },
  });
}

/** Opens the delete dialog and types the workspace name back. */
async function openAndConfirmName(): Promise<void> {
  await userEvent.click(
    await screen.findByRole('button', { name: 'Delete from WebbPulse' })
  );
  await screen.findByRole('form', { name: 'Delete the workspace' });
  await userEvent.type(
    screen.getByLabelText('Type platform to confirm'),
    'platform'
  );
}

/** The submit button inside the delete dialog. */
function submitButton(): HTMLElement {
  const form = screen.getByRole('form', { name: 'Delete the workspace' });
  const button = form.querySelector('button[type="submit"]');
  if (!(button instanceof HTMLElement)) {
    throw new Error('No submit button in the delete form');
  }
  return button;
}

describe('DeletionSettings', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.listConfigVersions.mockResolvedValue({
      items: [aConfigVersion()],
    });
    apiMock.listRuns.mockResolvedValue({ items: [aRun('applied')] });
  });

  it('safe deletes once the name is typed back', async () => {
    apiMock.deleteWorkspace.mockResolvedValue(undefined);
    renderDeletion();
    await userEvent.click(
      await screen.findByRole('button', { name: 'Delete from WebbPulse' })
    );
    expect(submitButton()).toBeDisabled();
    await userEvent.type(
      screen.getByLabelText('Type platform to confirm'),
      'platform'
    );
    await userEvent.click(submitButton());

    expect(apiMock.deleteWorkspace).toHaveBeenCalledWith(WORKSPACE_ID, {
      force: false,
    });
    expect(await screen.findByText('Workspaces list')).toBeInTheDocument();
  });

  it('offers a force delete behind a second confirmation when resources remain', async () => {
    apiMock.deleteWorkspace
      .mockRejectedValueOnce(
        conflict(
          'WORKSPACE_MANAGES_RESOURCES',
          'This workspace still manages resources.'
        )
      )
      .mockResolvedValueOnce(undefined);
    renderDeletion();
    await openAndConfirmName();
    await userEvent.click(submitButton());

    expect(await screen.findByTestId('force-delete')).toHaveTextContent(
      'Destroy infrastructure'
    );
    expect(submitButton()).toHaveTextContent('Force delete');
    expect(submitButton()).toBeDisabled();
    await userEvent.type(
      screen.getByLabelText('Type force delete to force the delete'),
      'force delet'
    );
    expect(submitButton()).toBeDisabled();
    await userEvent.type(
      screen.getByLabelText('Type force delete to force the delete'),
      'e'
    );
    await userEvent.click(submitButton());

    expect(apiMock.deleteWorkspace).toHaveBeenLastCalledWith(WORKSPACE_ID, {
      force: true,
    });
    expect(await screen.findByText('Workspaces list')).toBeInTheDocument();
  });

  it('shows why an active run blocks the delete', async () => {
    apiMock.deleteWorkspace.mockRejectedValue(
      conflict(
        'WORKSPACE_HAS_ACTIVE_RUN',
        'Run run-1 is still applying. Wait for it to finish or cancel it, then delete the workspace.'
      )
    );
    renderDeletion();
    await openAndConfirmName();
    await userEvent.click(submitButton());

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Run run-1 is still applying'
    );
    expect(screen.queryByTestId('force-delete')).not.toBeInTheDocument();
  });
});
