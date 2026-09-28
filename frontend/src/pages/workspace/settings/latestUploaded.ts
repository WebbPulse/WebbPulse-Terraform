/** Which configuration version a destroy plan runs against. */

import type { ConfigVersion } from '../../../api';

/** The newest uploaded configuration version, or null when there is none. */
export function latestUploaded(
  versions: readonly ConfigVersion[]
): ConfigVersion | null {
  const uploaded = versions
    .filter((version) => version.status === 'uploaded')
    .sort((left, right) => right.created_at.localeCompare(left.created_at));
  return uploaded[0] ?? null;
}
