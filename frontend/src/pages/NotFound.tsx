/** The catch-all route. */

import { Link } from 'react-router-dom';

/** The not found page. */
export function NotFound(): React.ReactElement {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-center">
      <p className="font-mono text-xs text-text-faint">404</p>
      <h1 className="mt-2 text-lg font-semibold text-text-strong">Not found</h1>
      <p className="mt-1 text-sm text-text-muted">That page does not exist.</p>
      <Link
        to="/workspaces"
        className="mt-6 inline-flex h-8 items-center rounded-md border border-line-strong bg-panel px-3 text-sm text-text hover:bg-raised"
      >
        Back to workspaces
      </Link>
    </div>
  );
}
