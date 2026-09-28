import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { RequireGuest } from '../components';
import {
  jsonResponse,
  renderWithAuth,
  stubAuthClient,
  unauthorizedResponse,
} from '../test-helpers/renderWithAuth';
import { SignIn } from './SignIn';

/** The sign-in route behind its real guard, as `App` mounts it. */
function signInRoutes(): React.ReactElement {
  return (
    <Routes>
      <Route
        path="/sign-in"
        element={
          <RequireGuest>
            <SignIn />
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
  return new URL(url).pathname;
}

describe('SignIn', () => {
  it('asks for the code when the password leg answers with a challenge', async () => {
    const calls: string[] = [];
    const client = stubAuthClient({
      user: { email: 'owner@webbpulse.com' },
      fetch: (input) => {
        const path = pathOf(input);
        calls.push(path);
        if (path === '/api/auth/login') {
          return Promise.resolve(
            jsonResponse({
              mfa_required: true,
              mfa_ticket: 'ticket-1',
              factors: ['totp'],
            })
          );
        }
        if (path === '/api/auth/login/totp') {
          return Promise.resolve(
            jsonResponse({ access_token: 'token', expires_in: 3600 })
          );
        }
        return Promise.resolve(unauthorizedResponse());
      },
    });
    const user = userEvent.setup();

    renderWithAuth(signInRoutes(), client, ['/sign-in']);

    await user.type(
      await screen.findByLabelText('Email'),
      'owner@webbpulse.com'
    );
    await user.type(screen.getByLabelText('Password'), 'correct horse');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(
      await screen.findByRole('heading', { name: 'Second factor' })
    ).toBeInTheDocument();
    expect(client.getState().hasAccessToken).toBe(false);
    expect(screen.queryByText('Workspaces')).not.toBeInTheDocument();

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

  it('keeps the form mounted while the password leg is in flight', async () => {
    const pending: { answer: (response: Response) => void } = {
      answer: () => undefined,
    };
    const client = stubAuthClient({
      fetch: (input) => {
        if (pathOf(input) === '/api/auth/login') {
          return new Promise<Response>((resolve) => {
            pending.answer = resolve;
          });
        }
        return Promise.resolve(unauthorizedResponse());
      },
    });
    const user = userEvent.setup();

    renderWithAuth(signInRoutes(), client, ['/sign-in']);

    await user.type(
      await screen.findByLabelText('Email'),
      'owner@webbpulse.com'
    );
    await user.type(screen.getByLabelText('Password'), 'correct horse');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(
      screen.queryByRole('status', { name: 'Checking your session' })
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toHaveValue('owner@webbpulse.com');

    pending.answer(
      jsonResponse({
        mfa_required: true,
        mfa_ticket: 'ticket-1',
        factors: ['totp'],
      })
    );

    expect(
      await screen.findByRole('heading', { name: 'Second factor' })
    ).toBeInTheDocument();
  });
});
