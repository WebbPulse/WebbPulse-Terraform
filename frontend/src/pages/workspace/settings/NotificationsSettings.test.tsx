import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from '@webbpulse/api-client';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { Layout } from '../../../components/Layout';
import {
  aConfigVersion,
  aDelivery,
  aNotification,
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
const NOTIFICATION_ID = 'nc-01J000000000000000000000';

/** Mounts the notifications settings page inside the shell. */
function renderNotifications(): void {
  renderWithAuth(
    <Routes>
      <Route element={<Layout />}>{workspaceRoutes()}</Route>
    </Routes>,
    signedInAuthClient(),
    [`/workspaces/${WORKSPACE_ID}/settings/notifications`]
  );
}

/** The notifications table, once it has rendered. */
async function table(): Promise<HTMLElement> {
  return screen.findByRole('table', { name: 'Notifications' });
}

/** An error the API client would throw for the given status. */
function apiError(
  status: number,
  message: string,
  retryAfterSeconds?: number
): ApiError {
  return new ApiError({
    status,
    statusText: 'Refused',
    url: `https://api.test/api/v1/workspaces/${WORKSPACE_ID}/notification-configurations`,
    method: 'POST',
    body: { success: false, status, message, request_id: 'r-1' },
    retryAfterSeconds,
  });
}

describe('NotificationsSettings', () => {
  beforeEach(() => {
    resetApiMock();
    apiMock.getWorkspace.mockResolvedValue(aWorkspace());
    apiMock.listConfigVersions.mockResolvedValue({
      items: [aConfigVersion()],
    });
    apiMock.listRuns.mockResolvedValue({ items: [aRun('applied')] });
    apiMock.listNotificationConfigurations.mockResolvedValue({
      items: [aNotification()],
    });
  });

  it('is linked from the settings rail', async () => {
    renderNotifications();
    const [link] = await screen.findAllByRole('link', {
      name: 'Notifications',
    });
    expect(link).toHaveAttribute(
      'href',
      `/workspaces/${WORKSPACE_ID}/settings/notifications`
    );
  });

  it('lists each configuration with its destination, triggers, state and last delivery', async () => {
    apiMock.listNotificationConfigurations.mockResolvedValue({
      items: [
        aNotification({
          last_delivery: aDelivery({ status: 'failed', status_code: 404 }),
        }),
      ],
    });
    renderNotifications();
    const rows = within(await table()).getAllByRole('row');
    const row = rows[1]!;

    expect(row).toHaveTextContent('Team channel');
    expect(row).toHaveTextContent('Slack');
    expect(row).toHaveTextContent('https://hooks.slack.com/****');
    expect(row).toHaveTextContent('Needs attention');
    expect(row).toHaveTextContent('Errored');
    expect(within(row).getByTestId('last-delivery')).toHaveTextContent(
      'Failed, HTTP 404'
    );
    expect(
      within(row).getByRole('switch', { name: 'Enable Team channel' })
    ).toHaveAttribute('aria-checked', 'true');
  });

  it('shows an empty state with nothing configured', async () => {
    apiMock.listNotificationConfigurations.mockResolvedValue({ items: [] });
    renderNotifications();
    expect(
      await screen.findByText('No notifications yet.')
    ).toBeInTheDocument();
  });

  it('creates a Discord notification on the chosen triggers', async () => {
    apiMock.listNotificationConfigurations.mockResolvedValue({ items: [] });
    apiMock.createNotificationConfiguration.mockResolvedValue(
      aNotification({ destination_type: 'discord' })
    );
    renderNotifications();
    await userEvent.click(
      await screen.findByRole('button', { name: '+ Create notification' })
    );
    const form = screen.getByRole('form', { name: 'Create a notification' });
    await userEvent.type(within(form).getByLabelText('Name'), 'Alerts');
    await userEvent.click(within(form).getByRole('tab', { name: 'Discord' }));
    await userEvent.type(
      within(form).getByLabelText('Webhook URL'),
      'https://discord.com/api/webhooks/1/abc'
    );
    expect(within(form).queryByLabelText('Token')).not.toBeInTheDocument();
    await userEvent.click(
      within(form).getByRole('checkbox', { name: /^Errored/ })
    );
    await userEvent.click(
      within(form).getByRole('checkbox', { name: /^Created/ })
    );
    await userEvent.click(
      within(form).getByRole('button', { name: 'Create notification' })
    );

    expect(apiMock.createNotificationConfiguration).toHaveBeenCalledWith(
      WORKSPACE_ID,
      {
        name: 'Alerts',
        destination_type: 'discord',
        url: 'https://discord.com/api/webhooks/1/abc',
        triggers: ['run:created', 'run:errored'],
        enabled: true,
      }
    );
    await waitFor(() => {
      expect(
        screen.queryByRole('form', { name: 'Create a notification' })
      ).not.toBeInTheDocument();
    });
  });

  it('sends a generic webhook token only for a generic webhook', async () => {
    apiMock.listNotificationConfigurations.mockResolvedValue({ items: [] });
    apiMock.createNotificationConfiguration.mockResolvedValue(
      aNotification({ destination_type: 'generic', has_token: true })
    );
    renderNotifications();
    await userEvent.click(
      await screen.findByRole('button', { name: '+ Create notification' })
    );
    const form = screen.getByRole('form', { name: 'Create a notification' });
    await userEvent.type(within(form).getByLabelText('Name'), 'Hook');
    await userEvent.click(within(form).getByRole('tab', { name: 'Webhook' }));
    await userEvent.type(
      within(form).getByLabelText('Webhook URL'),
      'https://example.com/hook'
    );
    await userEvent.type(within(form).getByLabelText('Token'), 's3cret');
    await userEvent.click(
      within(form).getByRole('button', { name: 'Create notification' })
    );

    expect(apiMock.createNotificationConfiguration).toHaveBeenCalledWith(
      WORKSPACE_ID,
      expect.objectContaining({
        destination_type: 'generic',
        token: 's3cret',
      })
    );
  });

  it('asks for a name and URL before creating', async () => {
    apiMock.listNotificationConfigurations.mockResolvedValue({ items: [] });
    renderNotifications();
    await userEvent.click(
      await screen.findByRole('button', { name: '+ Create notification' })
    );
    const form = screen.getByRole('form', { name: 'Create a notification' });
    await userEvent.type(within(form).getByLabelText('Name'), 'Alerts');
    await userEvent.click(
      within(form).getByRole('button', { name: 'Create notification' })
    );

    expect(
      within(form).getByText('Enter the webhook URL.')
    ).toBeInTheDocument();
    expect(apiMock.createNotificationConfiguration).not.toHaveBeenCalled();
  });

  it('shows the workspace limit when the API refuses with a 409', async () => {
    apiMock.listNotificationConfigurations.mockResolvedValue({ items: [] });
    apiMock.createNotificationConfiguration.mockRejectedValue(
      apiError(
        409,
        'A workspace can hold at most 50 notification configurations.'
      )
    );
    renderNotifications();
    await userEvent.click(
      await screen.findByRole('button', { name: '+ Create notification' })
    );
    const form = screen.getByRole('form', { name: 'Create a notification' });
    await userEvent.type(within(form).getByLabelText('Name'), 'Alerts');
    await userEvent.type(
      within(form).getByLabelText('Webhook URL'),
      'https://hooks.slack.com/services/T/B/x'
    );
    await userEvent.click(
      within(form).getByRole('button', { name: 'Create notification' })
    );

    expect(await within(form).findByRole('alert')).toHaveTextContent(
      'at most 50 notification configurations'
    );
  });

  it('keeps the stored URL when an edit leaves it blank', async () => {
    apiMock.updateNotificationConfiguration.mockResolvedValue(aNotification());
    renderNotifications();
    await userEvent.click(
      within(await table()).getByRole('button', { name: 'Edit' })
    );
    const form = screen.getByRole('form', { name: 'Edit the notification' });
    expect(
      within(form).getByText(
        'Leave blank to keep https://hooks.slack.com/****.'
      )
    ).toBeInTheDocument();
    const name = within(form).getByLabelText('Name');
    await userEvent.clear(name);
    await userEvent.type(name, 'Ops channel');
    await userEvent.click(
      within(form).getByRole('button', { name: 'Save notification' })
    );

    expect(apiMock.updateNotificationConfiguration).toHaveBeenCalledWith(
      WORKSPACE_ID,
      NOTIFICATION_ID,
      {
        name: 'Ops channel',
        triggers: ['run:needs_attention', 'run:errored'],
        enabled: true,
      }
    );
  });

  it('clears a generic webhook token with an empty string', async () => {
    apiMock.listNotificationConfigurations.mockResolvedValue({
      items: [
        aNotification({
          destination_type: 'generic',
          url_masked: 'https://example.com/****',
          has_token: true,
        }),
      ],
    });
    apiMock.updateNotificationConfiguration.mockResolvedValue(aNotification());
    renderNotifications();
    await userEvent.click(
      within(await table()).getByRole('button', { name: 'Edit' })
    );
    const form = screen.getByRole('form', { name: 'Edit the notification' });
    expect(
      within(form).getByText('A token is set. Leave blank to keep it.')
    ).toBeInTheDocument();
    await userEvent.click(
      within(form).getByRole('checkbox', { name: 'Remove the token' })
    );
    await userEvent.click(
      within(form).getByRole('button', { name: 'Save notification' })
    );

    expect(apiMock.updateNotificationConfiguration).toHaveBeenCalledWith(
      WORKSPACE_ID,
      NOTIFICATION_ID,
      expect.objectContaining({ token: '' })
    );
    const body = apiMock.updateNotificationConfiguration.mock.calls[0]?.[2];
    expect(body).not.toHaveProperty('url');
  });

  it('disables a configuration from its switch', async () => {
    apiMock.updateNotificationConfiguration.mockResolvedValue(
      aNotification({ enabled: false })
    );
    renderNotifications();
    await userEvent.click(
      within(await table()).getByRole('switch', { name: 'Enable Team channel' })
    );

    expect(apiMock.updateNotificationConfiguration).toHaveBeenCalledWith(
      WORKSPACE_ID,
      NOTIFICATION_ID,
      { enabled: false }
    );
  });

  it('deletes only after the confirmation', async () => {
    apiMock.deleteNotificationConfiguration.mockResolvedValue(undefined);
    renderNotifications();
    await userEvent.click(
      within(await table()).getByRole('button', { name: 'Delete' })
    );
    const dialog = await screen.findByRole('dialog');
    expect(apiMock.deleteNotificationConfiguration).not.toHaveBeenCalled();
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Delete notification' })
    );

    expect(apiMock.deleteNotificationConfiguration).toHaveBeenCalledWith(
      WORKSPACE_ID,
      NOTIFICATION_ID
    );
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
  });

  it('shows what a test send came back with', async () => {
    apiMock.verifyNotificationConfiguration.mockResolvedValue(
      aDelivery({
        status: 'failed',
        status_code: 403,
        error: 'The receiver refused the delivery.',
        response_excerpt: 'invalid_token',
      })
    );
    renderNotifications();
    await userEvent.click(
      within(await table()).getByRole('button', { name: 'Send test' })
    );

    const result = await screen.findByTestId('test-result');
    expect(apiMock.verifyNotificationConfiguration).toHaveBeenCalledWith(
      WORKSPACE_ID,
      NOTIFICATION_ID
    );
    expect(result).toHaveTextContent('Test send: Failed, HTTP 403');
    expect(result).toHaveTextContent('The receiver refused the delivery.');
    expect(result).toHaveTextContent('invalid_token');
  });

  it('says how long to wait when test sends are rate limited', async () => {
    apiMock.verifyNotificationConfiguration.mockRejectedValue(
      apiError(429, 'Too many test deliveries.', 42)
    );
    renderNotifications();
    await userEvent.click(
      within(await table()).getByRole('button', { name: 'Send test' })
    );

    expect(await screen.findByTestId('test-result')).toHaveTextContent(
      'Try again in 42 seconds.'
    );
  });
});
