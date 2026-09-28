/** How an installation's repository access reads in a table cell. */

import type { Installation } from '../../api';

/** An installation's repository access: all of them, or how many were selected. */
export function repositoryAccess(installation: Installation): string {
  if (installation.repository_selection === 'all') {
    return 'All repositories';
  }
  const count = installation.repository_count;
  return count === null || count === undefined
    ? 'Selected repositories'
    : `${String(count)} selected`;
}
