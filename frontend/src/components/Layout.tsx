/** The signed in shell: a left rail of navigation and the routed page beside it. */

import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { useAuth } from '@webbpulse/auth/react';

import { Button } from './Button';

/** The sections in the rail, in order. */
const SECTIONS: readonly {
  to: string;
  label: string;
  icon: React.ReactNode;
}[] = [
  {
    to: '/workspaces',
    label: 'Workspaces',
    icon: (
      <svg viewBox="0 0 16 16" className="size-4" fill="none">
        <rect
          x="2"
          y="2"
          width="5"
          height="5"
          rx="1"
          stroke="currentColor"
          strokeWidth="1.4"
        />
        <rect
          x="9"
          y="2"
          width="5"
          height="5"
          rx="1"
          stroke="currentColor"
          strokeWidth="1.4"
        />
        <rect
          x="2"
          y="9"
          width="5"
          height="5"
          rx="1"
          stroke="currentColor"
          strokeWidth="1.4"
        />
        <rect
          x="9"
          y="9"
          width="5"
          height="5"
          rx="1"
          stroke="currentColor"
          strokeWidth="1.4"
        />
      </svg>
    ),
  },
  {
    to: '/runs',
    label: 'Runs',
    icon: (
      <svg viewBox="0 0 16 16" className="size-4" fill="none">
        <path
          d="M4 3.5v9l8-4.5-8-4.5Z"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinejoin="round"
        />
      </svg>
    ),
  },
];

/** The email on the session, if the user record carries one. */
function emailOf(user: unknown): string | null {
  if (typeof user === 'object' && user !== null && 'email' in user) {
    const email = (user as { email?: unknown }).email;
    return typeof email === 'string' ? email : null;
  }
  return null;
}

/** The rail, the page and a skip link ahead of both. */
export function Layout(): React.ReactElement {
  const { logout, user } = useAuth();
  const navigate = useNavigate();
  const email = emailOf(user);

  const signOut = async (): Promise<void> => {
    await logout();
    void navigate('/sign-in', { replace: true });
  };

  return (
    <div className="flex min-h-screen">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-brand-600 focus:px-3 focus:py-1.5 focus:text-sm focus:text-white"
      >
        Skip to content
      </a>
      <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col border-r border-line bg-panel md:flex">
        <div className="flex h-12 items-center gap-2 border-b border-line px-4">
          <Mark />
          <span className="text-sm font-semibold text-text-strong">
            Terraform
          </span>
        </div>
        <nav aria-label="Primary" className="flex-1 space-y-0.5 p-2">
          {SECTIONS.map((section) => (
            <RailLink key={section.to} {...section} />
          ))}
        </nav>
        <div className="space-y-2 border-t border-line p-3">
          {email === null ? null : (
            <p className="truncate px-1 text-xs text-text-faint" title={email}>
              {email}
            </p>
          )}
          <Button
            variant="ghost"
            size="sm"
            className="w-full justify-start"
            onClick={() => {
              void signOut();
            }}
          >
            Sign out
          </Button>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-12 items-center justify-between gap-3 border-b border-line bg-bg/90 px-4 backdrop-blur md:hidden">
          <div className="flex items-center gap-2">
            <Mark />
            <span className="text-sm font-semibold text-text-strong">
              Terraform
            </span>
          </div>
          <nav aria-label="Primary" className="flex items-center gap-1">
            {SECTIONS.map((section) => (
              <RailLink key={section.to} {...section} compact />
            ))}
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                void signOut();
              }}
            >
              Sign out
            </Button>
          </nav>
        </header>
        <main
          id="main"
          className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 md:px-8"
        >
          <Outlet />
        </main>
      </div>
    </div>
  );
}

/** One link in the rail, with the section's icon and an active state. */
function RailLink({
  to,
  label,
  icon,
  compact = false,
}: {
  to: string;
  label: string;
  icon: React.ReactNode;
  compact?: boolean;
}): React.ReactElement {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `flex items-center gap-2 rounded-md text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
          compact ? 'px-2 py-1' : 'px-2.5 py-1.5'
        } ${
          isActive
            ? 'bg-raised text-text-strong'
            : 'text-text-muted hover:bg-raised/60 hover:text-text-strong'
        }`
      }
    >
      <span className="text-text-faint" aria-hidden="true">
        {icon}
      </span>
      {label}
    </NavLink>
  );
}

/** The small square mark beside the product name. */
export function Mark({
  className = '',
}: {
  className?: string;
}): React.ReactElement {
  return (
    <span
      aria-hidden="true"
      className={`inline-flex size-5 items-center justify-center rounded bg-brand-600 text-[10px] font-bold text-white ${className}`}
    >
      T
    </span>
  );
}
