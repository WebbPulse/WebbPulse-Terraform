import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import {
  jsonResponse,
  renderWithAuth,
  stubAuthClient,
} from '../../test-helpers/renderWithAuth';
import { Security, refusalMessage } from './Security';

const SECRET = 'JBSWY3DPEHPK3PXP';

const URI = `otpauth://totp/WebbPulse%20Terraform:owner?secret=${SECRET}&issuer=WebbPulse%20Terraform`;

/** A signed in client whose MFA routes answer from `routes`, keyed by path. */
function clientWith(routes: Record<string, () => Response>): {
  client: ReturnType<typeof stubAuthClient>;
  calls: string[];
} {
  const calls: string[] = [];
  const client = stubAuthClient({
    user: { email: 'owner@webbpulse.com' },
    fetch: (input) => {
      const url = new URL(
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.href
            : input.url
      );
      calls.push(url.pathname);
      const route = routes[url.pathname];
      return Promise.resolve(
        route === undefined
          ? jsonResponse({ access_token: 'test-token', expires_in: 3600 })
          : route()
      );
    },
  });
  return { client, calls };
}

/** Mounts the page at its route. */
function renderPage(client: ReturnType<typeof stubAuthClient>): void {
  renderWithAuth(
    <Routes>
      <Route path="/settings/security" element={<Security />} />
    </Routes>,
    client,
    ['/settings/security']
  );
}

describe('refusalMessage', () => {
  it('prefers the server sentence and falls back per reason', () => {
    expect(refusalMessage({ reason: 'invalid-code', message: 'Nope.' })).toBe(
      'Nope.'
    );
    expect(refusalMessage({ reason: 'invalid-code', message: '' })).toBe(
      'That code is not valid. Check the app and try again.'
    );
    expect(
      refusalMessage({ reason: 'rate-limited', message: '', retryAfter: 30 })
    ).toMatch(/Try again in 30 seconds\.$/);
    expect(refusalMessage({ reason: 'other', message: ' ' })).toBe(
      'That request was refused.'
    );
  });
});

describe('Security', () => {
  it('enrols, activates and shows the recovery codes once', async () => {
    const user = userEvent.setup();
    const { client, calls } = clientWith({
      '/api/auth/totp/enrol': () =>
        jsonResponse({ secret: SECRET, provisioning_uri: URI }),
      '/api/auth/totp/activate': () =>
        jsonResponse({ recovery_codes: ['aaaa-bbbb', 'cccc-dddd'] }),
    });
    renderPage(client);

    await user.click(
      await screen.findByRole('button', {
        name: 'Set up an authenticator app',
      })
    );
    expect(await screen.findByTestId('totp-secret')).toHaveTextContent(SECRET);
    expect(
      screen.getByRole('img', { name: 'QR code for the authenticator app' })
    ).toBeInTheDocument();

    await user.type(
      screen.getByLabelText('Authenticator or recovery code'),
      '123456'
    );
    await user.click(screen.getByRole('button', { name: 'Turn on' }));

    const codes = await screen.findByTestId('recovery-codes');
    expect(codes).toHaveTextContent('aaaa-bbbb');
    expect(codes).toHaveTextContent('cccc-dddd');
    expect(calls).toContain('/api/auth/totp/activate');

    const done = screen.getByRole('button', { name: 'Done' });
    expect(done).toBeDisabled();
    await user.click(screen.getByLabelText('I have saved these codes'));
    await user.click(done);

    await waitFor(() => {
      expect(screen.queryByTestId('recovery-codes')).not.toBeInTheDocument();
    });
    expect(screen.getByTestId('factor-status')).toHaveTextContent(
      'An authenticator app is on for this account.'
    );
  });

  it('shows a sentence for a refused code and keeps the same setup key', async () => {
    const user = userEvent.setup();
    const { client } = clientWith({
      '/api/auth/totp/enrol': () =>
        jsonResponse({ secret: SECRET, provisioning_uri: URI }),
      '/api/auth/totp/activate': () =>
        jsonResponse(
          {
            success: false,
            status: 400,
            code: 'MFA_INVALID_CODE',
            message: '',
          },
          400
        ),
    });
    renderPage(client);

    await user.click(
      await screen.findByRole('button', {
        name: 'Set up an authenticator app',
      })
    );
    await user.type(
      await screen.findByLabelText('Authenticator or recovery code'),
      '000000'
    );
    await user.click(screen.getByRole('button', { name: 'Turn on' }));

    expect(await screen.findByRole('alert')).not.toBeEmptyDOMElement();
    expect(screen.getByTestId('totp-secret')).toHaveTextContent(SECRET);
  });
});
