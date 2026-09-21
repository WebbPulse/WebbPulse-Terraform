/** The AWS account settings page: the role the runner assumes. */

import { useWorkspace } from '../../workspaceContext';
import { ConnectAccountPanel } from '../ConnectAccountPanel';

/** The AWS account page. */
export function RunRoleSettings(): React.ReactElement {
  const { workspace, keys } = useWorkspace();
  return (
    <div className="max-w-3xl space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-text-strong">AWS account</h2>
        <p className="mt-1 max-w-prose text-sm text-text-muted">
          Every run assumes this role in your account. Create it with one of the
          snippets, then save its ARN and check the connection.
        </p>
      </div>
      <ConnectAccountPanel workspace={workspace} queryKey={keys.workspace} />
    </div>
  );
}
