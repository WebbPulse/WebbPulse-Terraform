/** The configuration versions page, over the shared list and upload form. */

import { useWorkspace } from '../workspaceContext';
import { ConfigVersionsTab } from './ConfigVersionsTab';

/** The configuration versions page. */
export function ConfigVersionsPage(): React.ReactElement {
  const { workspace, versions, versionsQuery, keys } = useWorkspace();
  return (
    <div className="space-y-4">
      <h2 className="text-sm font-semibold text-text-strong">
        Configuration versions
      </h2>
      <ConfigVersionsTab
        workspace={workspace}
        versions={versions}
        isLoading={versionsQuery.isLoading}
        error={versionsQuery.error}
        keys={keys}
      />
    </div>
  );
}
