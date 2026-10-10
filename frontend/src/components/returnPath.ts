/** Where sign-in sends a visitor back to. */

import { identityReturnUrl, safeReturnPath } from '@webbpulse/auth';
import type { Location } from 'react-router-dom';

/** The router state that carries where an anonymous visitor was headed. */
export interface ReturnState {
  from?: Pick<Location, 'pathname' | 'search'>;
}

/**
 * Where to send a visitor once signed in: the page they were sent away from,
 * such as a `terraform login` approval with its query, or the workspaces list.
 * Only an in-app path other than the sign-in page is honoured.
 */
export function returnPath(state: unknown): string {
  const from = (state as ReturnState | null)?.from;
  const path = `${from?.pathname ?? ''}${from?.search ?? ''}`;
  return safeReturnPath(path, '/workspaces', { excludePaths: ['/sign-in'] });
}

/** Where the identity service serves the `wp-tf login` approval page. */
export const DEVICE_APPROVAL_PATH = '/api/auth/device';

/** What a sign-in hand-off from the device approval page asks for. */
export interface DeviceHandOff {
  /** The approval page to send the browser back to once signed in. */
  returnTo: string;
  /** Whether a fresh sign-in is required even with a live session. */
  reauthenticate: boolean;
}

/**
 * The device approval hand-off a sign-in URL carries, or null.
 *
 * The approval page sends a browser here with `returnTo`, and with
 * `prompt=login` when its session is too old to approve. Only the approval
 * page on the identity origin is honoured, with its query and nothing else,
 * so the parameter cannot be used to bounce a visitor anywhere else.
 */
export function deviceHandOff(
  search: string,
  identityOrigin: string,
  pageOrigin: string = window.location.origin
): DeviceHandOff | null {
  const params = new URLSearchParams(search);
  const returnTo = identityReturnUrl(params.get('returnTo'), {
    identityOrigin,
    path: DEVICE_APPROVAL_PATH,
    pageOrigin,
  });
  if (returnTo === null) {
    return null;
  }
  return {
    returnTo,
    reauthenticate: params.get('prompt') === 'login',
  };
}
