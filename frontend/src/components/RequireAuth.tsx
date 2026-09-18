/** The route guard every authenticated route sits behind. */

import { Navigate } from 'react-router-dom';
import { useAuth } from '@webbpulse/auth/react';
import type { ReactNode } from 'react';

import { Spinner } from './Spinner';

/** Props for {@link RequireAuth}. */
export interface RequireAuthProps {
  children: ReactNode;
}

/**
 * Holds a route back until the session has settled, then redirects an
 * anonymous visitor to the sign-in page.
 *
 * The spinner is gated on `isLoading`, which is true only until the session
 * settles once, and the redirect on `!isAuthenticated`. Gating either on
 * `isBusy` would unmount the tree on every later token call.
 */
export function RequireAuth({ children }: RequireAuthProps): ReactNode {
  const { isLoading, isAuthenticated } = useAuth();

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Restoring your session" />
      </div>
    );
  }
  if (!isAuthenticated) {
    return <Navigate to="/sign-in" replace />;
  }
  return children;
}

/**
 * Holds a guest route back until the session settles, then sends a signed in
 * visitor on to the workspaces list.
 *
 * A guest guard waits on `isBusy` as well as `isLoading`, so a sign-in form
 * mid-request is not thrown away and an MFA challenge is not lost with it.
 */
export function RequireGuest({ children }: RequireAuthProps): ReactNode {
  const { isLoading, isBusy, isAuthenticated } = useAuth();

  if (isLoading || isBusy) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Checking your session" />
      </div>
    );
  }
  if (isAuthenticated) {
    return <Navigate to="/workspaces" replace />;
  }
  return children;
}
