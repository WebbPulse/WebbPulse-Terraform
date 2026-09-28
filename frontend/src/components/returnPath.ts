/** Where sign-in sends a visitor back to. */

import type { Location } from 'react-router-dom';

/** The router state that carries where an anonymous visitor was headed. */
export interface ReturnState {
  from?: Pick<Location, 'pathname' | 'search'>;
}

/**
 * Where to send a visitor once signed in: the page they were sent away from,
 * such as a `terraform login` approval with its query, or the workspaces list.
 * Only an in-app path is honoured.
 */
export function returnPath(state: unknown): string {
  const from = (state as ReturnState | null)?.from;
  const path = `${from?.pathname ?? ''}${from?.search ?? ''}`;
  return path.startsWith('/') &&
    !path.startsWith('//') &&
    from?.pathname !== '/sign-in'
    ? path
    : '/workspaces';
}
