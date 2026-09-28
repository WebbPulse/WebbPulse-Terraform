/** Whether the signed in person holds the admin role, for hiding admin only navigation. */

import { useAuth } from '@webbpulse/auth/react';

/** The `roles` claim of a JWT, or an empty list when it cannot be read. */
export function rolesFromToken(token: string | null): string[] {
  const payload = token?.split('.')[1];
  if (payload === undefined || payload === '') {
    return [];
  }
  try {
    const padded = payload.replace(/-/g, '+').replace(/_/g, '/');
    const claims = JSON.parse(atob(padded)) as { roles?: unknown };
    const roles = claims.roles;
    if (Array.isArray(roles)) {
      return roles.map(String);
    }
    return typeof roles === 'string' && roles !== '' ? [roles] : [];
  } catch {
    return [];
  }
}

/** Whether a user record says it is an admin, for a loader that returns the flag. */
function userIsAdmin(user: unknown): boolean {
  if (typeof user !== 'object' || user === null) {
    return false;
  }
  const record = user as { is_admin?: unknown; roles?: unknown };
  return (
    record.is_admin === true ||
    (Array.isArray(record.roles) && record.roles.includes('admin'))
  );
}

/**
 * Whether the session is an admin's.
 *
 * Only ever a hint for what the rail shows: every admin route is enforced by the
 * API's `admin` scope, so a wrong answer here hides a link or shows one that
 * answers 403, and grants nothing.
 */
export function useIsAdmin(): boolean {
  const { user, getAccessToken } = useAuth();
  return (
    userIsAdmin(user) || rolesFromToken(getAccessToken()).includes('admin')
  );
}
