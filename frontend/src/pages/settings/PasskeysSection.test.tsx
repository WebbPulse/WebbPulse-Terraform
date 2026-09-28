import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import {
  jsonResponse,
  renderWithAuth,
  stubAuthClient,
  stubAuthenticator,
  type StubAuthenticator,
} from '../../test-helpers/renderWithAuth';
import { Security } from './Security';

/** One passkey as the list route renders it. */
function summary(id: string, name: string): Record<string, unknown> {
  return {
    credential_id: id,
    name,
    created_at: '2026-09-01T00:00:00Z',
    last_used_at: null,
    transports: ['internal'],
    aaguid: '',
    backup_eligible: false,
    backup_state: false,
    user_verified: true,
  };
}

/** The refusal envelope the identity routes answer with. */
function refusal(status: number, code: string, message: string): Response {
  return jsonResponse(
    { success: false, status, message, error_code: code, request_id: 'r' },
    status
  );
}

/** A fake identity backend holding the account's passkeys in memory. */
function backend(options: { disabled?: boolean } = {}): {
  client: ReturnType<typeof stubAuthClient>;
  calls: string[];
  webAuthn: StubAuthenticator;
} {
  const calls: string[] = [];
  const stored = new Map<string, Record<string, unknown>>([
    ['cred-1', summary('cred-1', 'Laptop')],
  ]);
  const webAuthn = stubAuthenticator('cred-2');
  const client = stubAuthClient({
    user: { email: 'owner@webbpulse.com' },
    webAuthn,
    fetch: (input, init) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      const path = new URL(url).pathname;
      const method = init?.method ?? 'GET';
      calls.push(`${method} ${path}`);
      if (path.startsWith('/api/auth/passkeys') && options.disabled === true) {
        return Promise.resolve(
          refusal(503, 'PASSKEYS_DISABLED', 'Passkeys are not available.')
        );
      }
      if (path === '/api/auth/passkeys' && method === 'GET') {
        return Promise.resolve(
          jsonResponse({ passkeys: [...stored.values()] })
        );
      }
      if (path === '/api/auth/passkeys/register/options') {
        return Promise.resolve(
          jsonResponse({ challenge_id: 'ch-1', publicKey: {} })
        );
      }
      if (path === '/api/auth/passkeys/register/verify') {
        const body = JSON.parse(
          typeof init?.body === 'string' ? init.body : '{}'
        ) as {
          name?: string;
        };
        const created = summary('cred-2', body.name ?? 'Passkey');
        stored.set('cred-2', created);
        return Promise.resolve(
          jsonResponse({ registered: true, passkey: created })
        );
      }
      const item = /^\/api\/auth\/passkeys\/([^/]+)$/.exec(path);
      if (item !== null) {
        const id = decodeURIComponent(item[1] ?? '');
        if (method === 'DELETE') {
          if (stored.size === 1) {
            return Promise.resolve(
              refusal(
                409,
                'LAST_CREDENTIAL',
                'This is your only way to sign in. Set a password before removing it.'
              )
            );
          }
          stored.delete(id);
          return Promise.resolve(jsonResponse({ deleted: true }));
        }
        if (method === 'PATCH') {
          const body = JSON.parse(
            typeof init?.body === 'string' ? init.body : '{}'
          ) as {
            name: string;
          };
          const renamed = { ...stored.get(id), name: body.name };
          stored.set(id, renamed);
          return Promise.resolve(jsonResponse({ passkey: renamed }));
        }
      }
      if (path === '/api/auth/totp') {
        return Promise.resolve(jsonResponse({ enabled: false }));
      }
      return Promise.resolve(
        jsonResponse({ access_token: 'test-token', expires_in: 3600 })
      );
    },
  });
  return { client, calls, webAuthn };
}

/** Mounts the security page at its route. */
function renderPage(client: ReturnType<typeof stubAuthClient>): void {
  renderWithAuth(
    <Routes>
      <Route path="/settings/security" element={<Security />} />
    </Routes>,
    client,
    ['/settings/security']
  );
}

beforeEach(() => {
  vi.stubGlobal('PublicKeyCredential', {
    isConditionalMediationAvailable: () => Promise.resolve(false),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Passkeys on the security page', () => {
  it('lists, adds, renames and removes passkeys', async () => {
    const user = userEvent.setup();
    const { client, calls, webAuthn } = backend();

    renderPage(client);

    const list = await screen.findByRole('list', { name: 'Passkeys' });
    expect(within(list).getByText('Laptop')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Add a passkey' }));
    await user.type(screen.getByLabelText('New passkey name'), 'Security key');
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByRole('status')).toHaveTextContent(
      'Passkey "Security key" added.'
    );
    expect(webAuthn.create).toHaveBeenCalledTimes(1);
    expect(
      within(screen.getByRole('list', { name: 'Passkeys' })).getByText(
        'Security key'
      )
    ).toBeInTheDocument();

    const [laptop] = screen.getAllByTestId('passkey-row');
    await user.click(
      within(laptop as HTMLElement).getByRole('button', { name: 'Rename' })
    );
    const name = screen.getByLabelText('Passkey name');
    await user.clear(name);
    await user.type(name, 'Work laptop');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('Work laptop')).toBeInTheDocument();
    expect(calls).toContain('PATCH /api/auth/passkeys/cred-1');

    await user.click(
      screen.getByRole('button', { name: 'Remove Security key' })
    );

    await waitFor(() => {
      expect(screen.queryByText('Security key')).not.toBeInTheDocument();
    });
    expect(calls).toContain('DELETE /api/auth/passkeys/cred-2');
  });

  it('shows the server sentence when the last passkey cannot go', async () => {
    const user = userEvent.setup();
    const { client } = backend();

    renderPage(client);

    await user.click(
      await screen.findByRole('button', { name: 'Remove Laptop' })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This is your only way to sign in.'
    );
    expect(screen.getByText('Laptop')).toBeInTheDocument();
  });

  it('hides the section where passkeys are off', async () => {
    const { client, calls } = backend({ disabled: true });

    renderPage(client);

    await waitFor(() => {
      expect(calls).toContain('GET /api/auth/passkeys');
    });
    await waitFor(() => {
      expect(
        screen.queryByRole('heading', { name: 'Passkeys' })
      ).not.toBeInTheDocument();
    });
    expect(
      screen.getByRole('heading', { name: 'Authenticator app' })
    ).toBeInTheDocument();
  });
});
