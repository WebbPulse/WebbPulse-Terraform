import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { resetAvailabilityCache } from '@webbpulse/discovery';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { RequireGuest } from '../components';
import {
  jsonResponse,
  renderWithAuth,
  stubAuthenticator,
  stubAuthClient,
  unauthorizedResponse,
  type StubAuthenticator,
} from '../test-helpers/renderWithAuth';
import { SignIn } from './SignIn';

/** The sign-in route behind its real guard, with passkey autofill off. */
function signInRoutes(): React.ReactElement {
  return (
    <Routes>
      <Route
        path="/sign-in"
        element={
          <RequireGuest>
            <SignIn conditionalPasskey={false} />
          </RequireGuest>
        }
      />
      <Route path="/workspaces" element={<p>Workspaces</p>} />
    </Routes>
  );
}

/** The path a stubbed `fetch` was called with. */
function pathOf(input: RequestInfo | URL): string {
  const url =
    typeof input === 'string'
      ? input
      : input instanceof URL
        ? input.href
        : input.url;
  return new URL(url, 'https://app.test').pathname;
}

/** Answers the availability probe the page sends through the global `fetch`. */
function deploymentOffering(passwordless: boolean): void {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) =>
      Promise.resolve(
        pathOf(input) === '/api/auth/passkeys/availability'
          ? jsonResponse({ enabled: true, passwordless })
          : unauthorizedResponse()
      )
    )
  );
}

/** A client whose identity routes answer from `routes`, keyed by path. */
function clientWith(routes: Record<string, () => Response>): {
  client: ReturnType<typeof stubAuthClient>;
  calls: string[];
  webAuthn: StubAuthenticator;
} {
  const calls: string[] = [];
  const webAuthn = stubAuthenticator();
  const client = stubAuthClient({
    user: { email: 'owner@webbpulse.com' },
    webAuthn,
    fetch: (input) => {
      const path = pathOf(input);
      calls.push(path);
      const route = routes[path];
      return Promise.resolve(
        route === undefined ? unauthorizedResponse() : route()
      );
    },
  });
  return { client, calls, webAuthn };
}

const OPTIONS = (): Response =>
  jsonResponse({ challenge_id: 'ch-1', publicKey: { challenge: 'Y2hhbA' } });

beforeEach(() => {
  resetAvailabilityCache();
  vi.stubGlobal('PublicKeyCredential', {
    isConditionalMediationAvailable: () => Promise.resolve(false),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('SignIn with a passkey', () => {
  it('hides the passkey option where passwordless sign-in is off', async () => {
    deploymentOffering(false);
    const { client } = clientWith({});

    renderWithAuth(signInRoutes(), client, ['/sign-in']);

    expect(await screen.findByLabelText('Email')).toBeInTheDocument();
    await waitFor(() => {
      expect(fetch).toHaveBeenCalled();
    });
    expect(
      screen.queryByRole('button', { name: 'Sign in with passkey' })
    ).not.toBeInTheDocument();
  });

  it('signs in and leaves the page', async () => {
    deploymentOffering(true);
    const { client, calls, webAuthn } = clientWith({
      '/api/auth/login/passkey/options': OPTIONS,
      '/api/auth/login/passkey/verify': () =>
        jsonResponse({ access_token: 'token', expires_in: 3600 }),
    });
    const user = userEvent.setup();

    renderWithAuth(signInRoutes(), client, ['/sign-in']);

    await user.click(
      await screen.findByRole('button', { name: 'Sign in with passkey' })
    );

    await waitFor(() => {
      expect(screen.getByText('Workspaces')).toBeInTheDocument();
    });
    expect(webAuthn.get).toHaveBeenCalledTimes(1);
    expect(calls).toContain('/api/auth/login/passkey/verify');
    expect(calls).not.toContain('/api/auth/login');
  });

  it('asks for the code when the passkey leg answers with a challenge', async () => {
    deploymentOffering(true);
    const { client, calls } = clientWith({
      '/api/auth/login/passkey/options': OPTIONS,
      '/api/auth/login/passkey/verify': () =>
        jsonResponse({
          mfa_required: true,
          mfa_ticket: 'ticket-1',
          factors: ['totp'],
        }),
      '/api/auth/login/totp': () =>
        jsonResponse({ access_token: 'token', expires_in: 3600 }),
    });
    const user = userEvent.setup();

    renderWithAuth(signInRoutes(), client, ['/sign-in']);

    await user.click(
      await screen.findByRole('button', { name: 'Sign in with passkey' })
    );

    expect(
      await screen.findByRole('heading', { name: 'Second factor' })
    ).toBeInTheDocument();
    expect(client.getState().hasAccessToken).toBe(false);

    await user.type(
      screen.getByLabelText('Authentication or recovery code'),
      '123456'
    );
    await user.click(screen.getByRole('button', { name: 'Verify' }));

    await waitFor(() => {
      expect(screen.getByText('Workspaces')).toBeInTheDocument();
    });
    expect(calls).toContain('/api/auth/login/totp');
  });

  it('shows the refusal and stays on the form', async () => {
    deploymentOffering(true);
    const { client } = clientWith({
      '/api/auth/login/passkey/options': OPTIONS,
      '/api/auth/login/passkey/verify': () =>
        jsonResponse(
          {
            success: false,
            status: 401,
            message: 'That passkey could not be verified.',
            error_code: 'PASSKEY_REJECTED',
            request_id: 'r-test',
          },
          401
        ),
    });
    const user = userEvent.setup();

    renderWithAuth(signInRoutes(), client, ['/sign-in']);

    await user.click(
      await screen.findByRole('button', { name: 'Sign in with passkey' })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'That passkey could not be verified.'
    );
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.queryByText('Workspaces')).not.toBeInTheDocument();
  });
});
