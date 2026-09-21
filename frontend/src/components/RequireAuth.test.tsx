import { screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import {
  jsonResponse,
  renderWithAuth,
  signedInAuthClient,
  stubAuthClient,
} from '../test-helpers/renderWithAuth';
import { RequireAuth, RequireGuest } from './RequireAuth';

/** The routes both guards are exercised through. */
function guardedRoutes(): React.ReactElement {
  return (
    <Routes>
      <Route
        path="/sign-in"
        element={
          <RequireGuest>
            <p>Sign in form</p>
          </RequireGuest>
        }
      />
      <Route
        path="/workspaces"
        element={
          <RequireAuth>
            <p>Workspaces</p>
          </RequireAuth>
        }
      />
    </Routes>
  );
}

describe('RequireAuth', () => {
  it('shows a spinner while the session is still unknown', () => {
    renderWithAuth(
      guardedRoutes(),
      stubAuthClient({
        fetch: () => new Promise(() => undefined),
      }),
      ['/workspaces']
    );

    expect(
      screen.getByRole('status', { name: 'Restoring your session' })
    ).toBeInTheDocument();
    expect(screen.queryByText('Workspaces')).not.toBeInTheDocument();
  });

  it('redirects an anonymous visitor to the sign-in page', async () => {
    renderWithAuth(guardedRoutes(), stubAuthClient(), ['/workspaces']);

    expect(await screen.findByText('Sign in form')).toBeInTheDocument();
    expect(screen.queryByText('Workspaces')).not.toBeInTheDocument();
  });

  it('renders the route once a session is held', async () => {
    renderWithAuth(guardedRoutes(), signedInAuthClient(), ['/workspaces']);

    expect(await screen.findByText('Workspaces')).toBeInTheDocument();
  });
});

describe('RequireGuest', () => {
  it('sends a signed in visitor on to the workspaces list', async () => {
    renderWithAuth(guardedRoutes(), signedInAuthClient(), ['/sign-in']);

    expect(await screen.findByText('Workspaces')).toBeInTheDocument();
  });

  it('shows the sign-in form to an anonymous visitor', async () => {
    renderWithAuth(guardedRoutes(), stubAuthClient(), ['/sign-in']);

    expect(await screen.findByText('Sign in form')).toBeInTheDocument();
  });

  it('waits while a session call is in flight rather than unmounting the form', async () => {
    const pending: { answer: (response: Response) => void } = {
      answer: () => undefined,
    };
    const client = stubAuthClient({
      fetch: () =>
        new Promise<Response>((resolve) => {
          pending.answer = resolve;
        }),
    });

    renderWithAuth(guardedRoutes(), client, ['/sign-in']);

    expect(
      screen.getByRole('status', { name: 'Checking your session' })
    ).toBeInTheDocument();

    pending.answer(jsonResponse({ access_token: 'token', expires_in: 3600 }));
    await waitFor(() => {
      expect(screen.getByText('Workspaces')).toBeInTheDocument();
    });
  });
});
