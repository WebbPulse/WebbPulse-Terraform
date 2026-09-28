/** The route guard every authenticated route sits behind. */

import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '@webbpulse/auth/react';
import type { ReactNode } from 'react';

import { returnPath, type ReturnState } from './returnPath';
import { Spinner } from './Spinner';

/** Props for {@link RequireAuth}. */
export interface RequireAuthProps {
  children: ReactNode;
}

/**
 * Holds a route back until the session has settled, then redirects an
 * anonymous visitor to the sign-in page, remembering where they were headed.
 *
 * The spinner is gated on `isLoading`, which is true only until the session
 * settles once, and the redirect on `!isAuthenticated`. Gating either on
 * `isBusy` would unmount the tree on every later token call.
 */
export function RequireAuth({ children }: RequireAuthProps): ReactNode {
  const { isLoading, isAuthenticated } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Restoring your session" />
      </div>
    );
  }
  if (!isAuthenticated) {
    const state: ReturnState = {
      from: { pathname: location.pathname, search: location.search },
    };
    return <Navigate to="/sign-in" replace state={state} />;
  }
  return children;
}

/**
 * Holds a guest route back until the session settles once, then sends a signed
 * in visitor on to the page they were headed for, or the workspaces list.
 *
 * The spinner is gated on `isLoading` alone, so a sign-in form stays mounted
 * while its own login call is in flight and keeps the MFA ticket that call
 * returns. The redirect also waits on `!isBusy`, so a second leg in flight is
 * not cut short by a navigation.
 */
export function RequireGuest({ children }: RequireAuthProps): ReactNode {
  const { isLoading, isBusy, isAuthenticated } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Checking your session" />
      </div>
    );
  }
  if (isAuthenticated && !isBusy) {
    return <Navigate to={returnPath(location.state)} replace />;
  }
  return children;
}
