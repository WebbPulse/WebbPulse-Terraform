/** The catch-all route. */

import { Link } from 'react-router-dom';

/** The not found page. */
export function NotFound(): React.ReactElement {
  return (
    <div className="space-y-3">
      <h1 className="text-xl font-semibold text-surface-50">Not found</h1>
      <p className="text-surface-300">That page does not exist.</p>
      <Link to="/workspaces" className="text-brand-300 hover:text-brand-200">
        Back to workspaces
      </Link>
    </div>
  );
}
