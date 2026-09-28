import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import type { ApiKey } from '../../api';
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

vi.mock('../../api/client', () => apiClientModuleMock());

const { ApiKeys, expiryBody, expiryFrom, keyStatus, scopesFromToken } =
  await import('./ApiKeys');

/** A stored key, active unless overridden. */
function aKey(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    key_id: 'a'.repeat(64),
    name: 'provider',
    prefix: 'wpk_abcd',
    scopes: ['workspaces:read'],
    created_at: '2026-09-01T00:00:00Z',
    expires_at: Math.floor(Date.now() / 1000) + 86_400,
    last_used_at: null,
    revoked_at: null,
    ...overrides,
  };
}

/** A token whose payload carries `scope`. */
function tokenWithScope(scope: string): string {
  const payload = btoa(JSON.stringify({ scope }))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
  return `header.${payload}.signature`;
}

/** Mounts the page at its route. */
function renderPage(client = signedInAuthClient()): void {
  renderWithAuth(
    <Routes>
      <Route path="/settings/api-keys" element={<ApiKeys />} />
    </Routes>,
    client,
    ['/settings/api-keys']
  );
}

describe('ApiKeys helpers', () => {
  it('reads the scope claim and tolerates a token it cannot parse', () => {
    expect(scopesFromToken(tokenWithScope('runs:read runs:write'))).toEqual([
      'runs:read',
      'runs:write',
    ]);
    expect(scopesFromToken('test-token')).toEqual([]);
    expect(scopesFromToken(null)).toEqual([]);
  });

  it('tells revoked and expired keys from active ones', () => {
    const now = Date.parse('2026-09-27T00:00:00Z');
    expect(keyStatus(aKey({ expires_at: now / 1000 + 60 }), now)).toBe(
      'active'
    );
    expect(keyStatus(aKey({ expires_at: now / 1000 - 60 }), now)).toBe(
      'expired'
    );
    expect(keyStatus(aKey({ revoked_at: '2026-09-02T00:00:00Z' }), now)).toBe(
      'revoked'
    );
  });

  it('builds a no expiry body or a dated one from the choice', () => {
    const now = Date.parse('2026-09-27T00:00:00Z');
    expect(expiryBody('never', now)).toEqual({ no_expiry: true });
    expect(expiryBody(30, now)).toEqual({ expires_at: expiryFrom(30, now) });
  });

  it('puts the expiry the given number of days out', () => {
    expect(expiryFrom(90, Date.parse('2026-09-27T00:00:00Z'))).toBe(
      '2026-12-26T00:00:00.000Z'
    );
  });
});

describe('ApiKeys', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('lists keys and says when there are none', async () => {
    apiMock.listApiKeys.mockResolvedValue({ items: [] });
    renderPage();
    expect(await screen.findByText('No API keys yet')).toBeInTheDocument();
  });

  it('renders each key without its secret, marking revoked ones', async () => {
    apiMock.listApiKeys.mockResolvedValue({
      items: [
        aKey(),
        aKey({
          key_id: 'b'.repeat(64),
          name: 'old agent',
          revoked_at: '2026-09-02T00:00:00Z',
        }),
      ],
    });
    renderPage();

    const table = await screen.findByRole('table', { name: 'API keys' });
    expect(within(table).getByText('provider')).toBeInTheDocument();
    expect(within(table).getAllByText('wpk_abcd...')).toHaveLength(2);
    expect(within(table).getByText('Revoked')).toBeInTheDocument();
    expect(
      within(table).getByRole('button', { name: 'Revoke provider' })
    ).toBeInTheDocument();
    expect(
      within(table).queryByRole('button', { name: 'Revoke old agent' })
    ).toBeNull();
  });

  it('creates a key with a 90 day default expiry and shows the secret once', async () => {
    apiMock.listApiKeys.mockResolvedValue({ items: [] });
    apiMock.createApiKey.mockResolvedValue({
      ...aKey({ name: 'ci' }),
      key: 'wpk_secretvalue',
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole('button', { name: 'Create an API key' })
    );
    const form = screen.getByRole('form', { name: 'Create an API key' });
    expect(within(form).getByLabelText('Expiration')).toHaveValue('90');
    await userEvent.type(within(form).getByLabelText('Name'), 'ci');
    await userEvent.click(
      within(form).getByRole('button', { name: 'Create API key' })
    );

    await waitFor(() => {
      expect(apiMock.createApiKey).toHaveBeenCalledTimes(1);
    });
    const body = apiMock.createApiKey.mock.calls[0]?.[0];
    expect(body?.name).toBe('ci');
    expect(body?.scopes).toBeNull();
    const days = (Date.parse(body?.expires_at ?? '') - Date.now()) / 86_400_000;
    expect(Math.round(days)).toBe(90);

    expect(await screen.findByTestId('new-api-key')).toHaveTextContent(
      'wpk_secretvalue'
    );
    expect(screen.queryByRole('dialog')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(screen.queryByTestId('new-api-key')).toBeNull();
  });

  it('creates a key with no expiry and warns it lasts until revoked', async () => {
    apiMock.listApiKeys.mockResolvedValue({ items: [] });
    apiMock.createApiKey.mockResolvedValue({
      ...aKey({ name: 'service', expires_at: null }),
      key: 'wpk_forever',
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole('button', { name: 'Create an API key' })
    );
    const form = screen.getByRole('form', { name: 'Create an API key' });
    expect(within(form).queryByRole('note')).toBeNull();
    await userEvent.selectOptions(
      within(form).getByLabelText('Expiration'),
      'No expiry'
    );
    expect(within(form).getByRole('note')).toHaveTextContent(
      'stays valid until you revoke it'
    );
    await userEvent.type(within(form).getByLabelText('Name'), 'service');
    await userEvent.click(
      within(form).getByRole('button', { name: 'Create API key' })
    );

    await waitFor(() => {
      expect(apiMock.createApiKey).toHaveBeenCalledTimes(1);
    });
    const body = apiMock.createApiKey.mock.calls[0]?.[0];
    expect(body?.no_expiry).toBe(true);
    expect(body?.expires_at).toBeUndefined();
  });

  it('shows Never for a key with no expiry', async () => {
    apiMock.listApiKeys.mockResolvedValue({
      items: [aKey({ expires_at: null, last_used_at: '2026-09-02T00:00:00Z' })],
    });
    renderPage();

    const table = await screen.findByRole('table', { name: 'API keys' });
    expect(within(table).getByText('Never')).toBeInTheDocument();
    expect(within(table).queryByText('Expired')).toBeNull();
  });

  it('narrows a key to chosen scopes from those the session holds', async () => {
    apiMock.listApiKeys.mockResolvedValue({ items: [] });
    apiMock.createApiKey.mockResolvedValue({
      ...aKey({ name: 'reader' }),
      key: 'wpk_secretvalue',
    });
    const client = stubAuthClient({
      fetch: () =>
        Promise.resolve(
          jsonResponse({
            access_token: tokenWithScope('workspaces:read runs:read'),
            expires_in: 3600,
          })
        ),
      user: { email: 'engineer@webbpulse.com' },
    });
    renderPage(client);

    await userEvent.click(
      await screen.findByRole('button', { name: 'Create an API key' })
    );
    const form = screen.getByRole('form', { name: 'Create an API key' });
    await userEvent.type(within(form).getByLabelText('Name'), 'reader');
    await userEvent.click(
      within(form).getByRole('radio', { name: 'Only the scopes I choose' })
    );
    const submit = within(form).getByRole('button', { name: 'Create API key' });
    expect(submit).toBeDisabled();
    await userEvent.click(
      within(form).getByRole('checkbox', { name: 'runs:read' })
    );
    await userEvent.click(submit);

    await waitFor(() => {
      expect(apiMock.createApiKey).toHaveBeenCalledTimes(1);
    });
    expect(apiMock.createApiKey.mock.calls[0]?.[0].scopes).toEqual([
      'runs:read',
    ]);
  });

  it('shows a refused create inside the dialog', async () => {
    apiMock.listApiKeys.mockResolvedValue({ items: [] });
    apiMock.createApiKey.mockRejectedValue(new Error('Too many keys.'));
    renderPage();

    await userEvent.click(
      await screen.findByRole('button', { name: 'Create an API key' })
    );
    const form = screen.getByRole('form', { name: 'Create an API key' });
    await userEvent.type(within(form).getByLabelText('Name'), 'ci');
    await userEvent.click(
      within(form).getByRole('button', { name: 'Create API key' })
    );

    expect(await within(form).findByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('revokes a key after confirmation', async () => {
    const key = aKey();
    apiMock.listApiKeys.mockResolvedValue({ items: [key] });
    apiMock.revokeApiKey.mockResolvedValue({
      ...key,
      revoked_at: '2026-09-27T00:00:00Z',
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole('button', { name: 'Revoke provider' })
    );
    const dialog = screen.getByRole('dialog', { name: 'Revoke API key' });
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Revoke key' })
    );

    await waitFor(() => {
      expect(apiMock.revokeApiKey).toHaveBeenCalledWith(key.key_id);
    });
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });
});
