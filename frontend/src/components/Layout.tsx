/** The signed in chrome: the site header and the routed outlet. */

import { Link, Outlet, useNavigate } from 'react-router-dom';
import { useAuth } from '@webbpulse/auth/react';

/** The header and the routed page beneath it. */
export function Layout(): React.ReactElement {
  const { logout } = useAuth();
  const navigate = useNavigate();

  const signOut = async (): Promise<void> => {
    await logout();
    void navigate('/sign-in', { replace: true });
  };

  return (
    <div className="min-h-screen">
      <header className="border-b border-surface-700 bg-surface-800">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-4 py-3">
          <Link to="/workspaces" className="font-semibold text-surface-50">
            WebbPulse Terraform
          </Link>
          <nav className="flex items-center gap-4 text-sm">
            <Link
              to="/workspaces"
              className="text-surface-200 hover:text-white"
            >
              Workspaces
            </Link>
            <Link to="/runs" className="text-surface-200 hover:text-white">
              Runs
            </Link>
            <button
              type="button"
              onClick={() => {
                void signOut();
              }}
              className="rounded-md border border-surface-600 px-2.5 py-1 text-surface-200 hover:text-white"
            >
              Sign out
            </button>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
