/** The pill that says where a module version's publishing stands. */

import type { ModuleVersionStatus } from '../../api';
import { STATUS_CLASSES, STATUS_LABELS } from './moduleText';

/** A tone-soft pill for one version status. */
export function VersionStatusBadge({
  status,
}: {
  status: ModuleVersionStatus;
}): React.ReactElement {
  return (
    <span
      data-testid="version-status"
      data-status={status}
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium whitespace-nowrap ${STATUS_CLASSES[status]}`}
    >
      {STATUS_LABELS[status]}
    </span>
  );
}
