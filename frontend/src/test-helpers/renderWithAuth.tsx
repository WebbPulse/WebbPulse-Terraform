/** The wrapper every page test mounts through: a router plus `AuthProvider`. */

import { render, type RenderResult } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { vi, type Mock } from 'vitest';
import type { ReactNode } from 'react';
import {
  createAuthClient,
  type AuthClient,
  type WebAuthnAdapter,
} from '@webbpulse/auth';
import { AuthProvider, type AnyAuthClient } from '@webbpulse/auth/react';

/** A JSON `Response`, which is what every route answers with. */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

/** The 401 envelope the backend answers with when there is no session. */
export function unauthorizedResponse(): Response {
  return jsonResponse(
    {
      success: false,
      status: 401,
      message: 'No session.',
      request_id: 'r-test',
    },
    401
  );
}

/** A stand-in authenticator whose calls a test can read back. */
export interface StubAuthenticator {
  create: Mock<(options: unknown) => Promise<unknown>>;
  get: Mock<(options: unknown) => Promise<unknown>>;
}

/** An authenticator that answers every ceremony with `credentialId`. */
export function stubAuthenticator(credentialId = 'cred-1'): StubAuthenticator {
  const credential = { id: credentialId, type: 'public-key' };
  return {
    create: vi.fn((_options: unknown) => Promise.resolve<unknown>(credential)),
    get: vi.fn((_options: unknown) => Promise.resolve<unknown>(credential)),
  };
}

/** Options for {@link stubAuthClient}. */
export interface StubAuthClientOptions {
  /** Answers the refresh and session calls. Defaults to a 401, so anonymous. */
  fetch?: typeof globalThis.fetch;
  /** The user the client reports once a token is held. */
  user?: unknown;
  /** Stands in for `navigator.credentials` in a passkey test. */
  webAuthn?: WebAuthnAdapter;
}

/**
 * A real `AuthClient` over a stubbed `fetch`.
 *
 * The real client rather than a fake, so a test exercises the state machine the
 * guards run against in a browser. Proactive refresh is off, since a timer
 * firing mid-test is noise rather than coverage.
 */
export function stubAuthClient(
  options: StubAuthClientOptions = {}
): AuthClient<unknown> {
  const {
    fetch = () => Promise.resolve(unauthorizedResponse()),
    user = null,
    webAuthn,
  } = options;

  return createAuthClient({
    baseUrl: 'https://api.test',
    disableProactiveRefresh: true,
    loadUser: () => Promise.resolve(user),
    clientOptions: { fetch, retries: 0 },
    ...(webAuthn === undefined ? {} : { webAuthn }),
  });
}

/** An auth client that reports a live session, for a page test. */
export function signedInAuthClient(): AuthClient<unknown> {
  return stubAuthClient({
    fetch: () =>
      Promise.resolve(
        jsonResponse({ access_token: 'test-token', expires_in: 3600 })
      ),
    user: { email: 'engineer@webbpulse.com' },
  });
}

/** Renders `children` inside a router and `AuthProvider`. */
export function renderWithAuth(
  children: ReactNode,
  client: AuthClient<unknown>,
  initialEntries: string[] = ['/']
): RenderResult {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <AuthProvider client={client as unknown as AnyAuthClient}>
        {children}
      </AuthProvider>
    </MemoryRouter>
  );
}
