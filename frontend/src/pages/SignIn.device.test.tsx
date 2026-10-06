import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';

import { API_BASE_URL, identityOriginFrom } from '../api';
import { RequireGuest } from '../components';
import {
  jsonResponse,
  renderWithAuth,
  signedInAuthClient,
  stubAuthClient,
  unauthorizedResponse,
} from '../test-helpers/renderWithAuth';
import { SignIn } from './SignIn';

vi.mock('../components/deviceNavigation', () => ({
  leaveForDeviceApproval: vi.fn(),
}));

const navigation = await import('../components/deviceNavigation');
const leave = vi.mocked(navigation.leaveForDeviceApproval);

const APPROVAL = `${identityOriginFrom(API_BASE_URL)}/api/auth/device?user_code=ABCD-EFGH`;

/** The sign-in URL the approval page hands a browser to. */
function signInUrl(returnTo: string, prompt?: string): string {
  const params = new URLSearchParams({ returnTo });
  if (prompt !== undefined) {
    params.set('prompt', prompt);
  }
  return `/sign-in?${params.toString()}`;
}

/** The sign-in route behind its real guard, as `App` mounts it. */
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

/** A client whose password leg signs straight in. */
function passwordClient(signedIn: boolean): ReturnType<typeof stubAuthClient> {
  let session = signedIn;
  return stubAuthClient({
    user: { email: 'owner@webbpulse.com' },
    fetch: (input) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      const path = new URL(url).pathname;
      if (path === '/api/auth/login') {
        session = true;
        return Promise.resolve(
          jsonResponse({ access_token: 'fresh', expires_in: 3600 })
        );
      }
      if (session && path === '/api/auth/refresh') {
        return Promise.resolve(
          jsonResponse({ access_token: 'old', expires_in: 3600 })
        );
      }
      return Promise.resolve(unauthorizedResponse());
    },
  });
}

/** Signs in through the password form. */
async function signInWithPassword(): Promise<void> {
  const user = userEvent.setup();
  await user.type(await screen.findByLabelText('Email'), 'owner@webbpulse.com');
  await user.type(screen.getByLabelText('Password'), 'correct horse');
  await user.click(screen.getByRole('button', { name: 'Sign in' }));
}

afterEach(() => {
  leave.mockReset();
});

describe('SignIn device hand-off', () => {
  it('sends a signed in visitor straight back to the approval page', async () => {
    renderWithAuth(signInRoutes(), signedInAuthClient(), [signInUrl(APPROVAL)]);

    await waitFor(() => {
      expect(leave).toHaveBeenCalledWith(APPROVAL);
    });
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument();
  });

  it('returns to the approval page after an anonymous visitor signs in', async () => {
    renderWithAuth(signInRoutes(), passwordClient(false), [
      signInUrl(APPROVAL),
    ]);

    await signInWithPassword();

    await waitFor(() => {
      expect(leave).toHaveBeenCalledWith(APPROVAL);
    });
    expect(screen.queryByText('Workspaces')).not.toBeInTheDocument();
  });

  it('asks a signed in visitor to sign in again under prompt=login', async () => {
    renderWithAuth(signInRoutes(), passwordClient(true), [
      signInUrl(APPROVAL, 'login'),
    ]);

    expect(await screen.findByLabelText('Email')).toBeInTheDocument();
    expect(leave).not.toHaveBeenCalled();

    await signInWithPassword();

    await waitFor(() => {
      expect(leave).toHaveBeenCalledWith(APPROVAL);
    });
  });

  it('ignores a returnTo outside the approval page', async () => {
    renderWithAuth(signInRoutes(), signedInAuthClient(), [
      signInUrl('https://evil.example/api/auth/device'),
    ]);

    expect(await screen.findByText('Workspaces')).toBeInTheDocument();
    expect(leave).not.toHaveBeenCalled();
  });
});
