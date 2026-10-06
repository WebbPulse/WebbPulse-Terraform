/** The route guard every authenticated route sits behind. */

import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '@webbpulse/auth/react';
import { useEffect, useMemo, type ReactNode } from 'react';

import { API_BASE_URL, identityOriginFrom } from '../api';
import { leaveForDeviceApproval } from './deviceNavigation';
import {
  deviceHandOff,
  returnPath,
  type DeviceHandOff,
  type ReturnState,
} from './returnPath';
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
 * The `wp-tf login` approval hand-off in the current URL, or null. Memoised on
 * the query string, so effects keyed on it run once per hand-off.
 */
export function useDeviceHandOff(): DeviceHandOff | null {
  const { search } = useLocation();
  return useMemo(
    () => deviceHandOff(search, identityOriginFrom(API_BASE_URL)),
    [search]
  );
}

/**
 * Holds a guest route back until the session settles once, then sends a signed
 * in visitor on to the page they were headed for, or the workspaces list.
 *
 * A device approval hand-off goes back to the identity service's approval page
 * instead, in a full navigation since it is not an app route. With
 * `prompt=login` the form stays up even for a signed in visitor, because the
 * approval needs a fresh sign-in, and the sign-in page sends them on.
 *
 * The spinner is gated on `isLoading` alone, so a sign-in form stays mounted
 * while its own login call is in flight and keeps the MFA ticket that call
 * returns. The redirect also waits on `!isBusy`, so a second leg in flight is
 * not cut short by a navigation.
 */
export function RequireGuest({ children }: RequireAuthProps): ReactNode {
  const { isLoading, isBusy, isAuthenticated } = useAuth();
  const location = useLocation();
  const handOff = useDeviceHandOff();
  const leaveForDevice =
    handOff !== null &&
    !handOff.reauthenticate &&
    !isLoading &&
    isAuthenticated &&
    !isBusy;

  useEffect(() => {
    if (leaveForDevice) {
      leaveForDeviceApproval(handOff.returnTo);
    }
  }, [leaveForDevice, handOff]);

  if (isLoading || leaveForDevice) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Checking your session" />
      </div>
    );
  }
  if (isAuthenticated && !isBusy && handOff === null) {
    return <Navigate to={returnPath(location.state)} replace />;
  }
  return children;
}
