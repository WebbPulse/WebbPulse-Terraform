/** The signed in shell: a left rail of navigation and the routed page beside it. */

import { useState } from 'react';
import { Outlet, useMatch, useNavigate } from 'react-router-dom';
import { useAuth } from '@webbpulse/auth/react';

import { ThemeToggle } from '../theme';
import { BrandMark, Wordmark } from './Brand';
import { Button } from './Button';
import { RailGroupLabel, RailLink } from './RailLink';
import { WorkspaceNav } from './WorkspaceNav';
import { useIsAdmin } from './useIsAdmin';
import { WorkspaceNavContext } from './workspaceNavContext';

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

/** The admin only sections under global settings. */
const SETTINGS_SECTIONS: readonly {
  to: string;
  label: string;
  icon: React.ReactNode;
}[] = [
  {
    to: '/settings/github',
    label: 'GitHub',
    icon: (
      <svg viewBox="0 0 16 16" className="size-4" fill="none">
        <circle
          cx="4.5"
          cy="3.5"
          r="1.5"
          stroke="currentColor"
          strokeWidth="1.4"
        />
        <circle
          cx="4.5"
          cy="12.5"
          r="1.5"
          stroke="currentColor"
          strokeWidth="1.4"
        />
        <circle
          cx="11.5"
          cy="5.5"
          r="1.5"
          stroke="currentColor"
          strokeWidth="1.4"
        />
        <path
          d="M4.5 5v6M11.5 7c0 2.5-3 2.5-7 4"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
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

/**
 * The rail, the page and a skip link ahead of both.
 *
 * Inside a workspace the rail gives way to that workspace's sections, the way
 * a workspace takes over the sidebar in the hosted product this follows.
 */
export function Layout(): React.ReactElement {
  const { logout, user } = useAuth();
  const navigate = useNavigate();
  const email = emailOf(user);
  const isAdmin = useIsAdmin();
  const sections = isAdmin ? [...SECTIONS, ...SETTINGS_SECTIONS] : SECTIONS;
  const workspaceMatch = useMatch('/workspaces/:workspaceId/*');
  const workspaceId = workspaceMatch?.params.workspaceId ?? null;
  const [workspaceName, setWorkspaceName] = useState<string | null>(null);

  const signOut = async (): Promise<void> => {
    await logout();
    void navigate('/sign-in', { replace: true });
  };

  return (
    <WorkspaceNavContext.Provider
      value={{ name: workspaceName, setName: setWorkspaceName }}
    >
      <div className="flex min-h-screen">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-accent focus:px-3 focus:py-1.5 focus:text-sm focus:text-accent-contrast"
        >
          Skip to content
        </a>
        <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col border-r border-line bg-panel md:flex">
          <div className="flex h-12 items-center justify-between gap-2 border-b border-line px-4">
            <Wordmark />
            <ThemeToggle />
          </div>
          {workspaceId === null ? (
            <nav aria-label="Primary" className="flex-1 space-y-0.5 p-2">
              <RailGroupLabel>Manage</RailGroupLabel>
              {SECTIONS.map((section) => (
                <RailLink key={section.to} {...section} />
              ))}
              {isAdmin ? (
                <>
                  <RailGroupLabel className="pt-4">Settings</RailGroupLabel>
                  {SETTINGS_SECTIONS.map((section) => (
                    <RailLink key={section.to} {...section} />
                  ))}
                </>
              ) : null}
            </nav>
          ) : (
            <WorkspaceNav workspaceId={workspaceId} name={workspaceName} />
          )}
          <div className="space-y-2 border-t border-line p-3">
            {email === null ? null : (
              <p
                className="truncate px-1 text-xs text-text-faint"
                title={email}
              >
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
          <header className="sticky top-0 z-30 border-b border-line bg-bg/90 backdrop-blur md:hidden">
            <div className="flex h-12 items-center justify-between gap-3 px-4">
              <Wordmark short />
              <nav aria-label="Primary" className="flex items-center gap-1">
                {sections.map((section) => (
                  <RailLink key={section.to} {...section} compact />
                ))}
                <ThemeToggle />
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
            </div>
            {workspaceId === null ? null : (
              <div className="border-t border-line px-2 py-1">
                <WorkspaceNav
                  workspaceId={workspaceId}
                  name={workspaceName}
                  compact
                />
              </div>
            )}
          </header>
          <main
            id="main"
            className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 md:px-8"
          >
            <Outlet />
          </main>
        </div>
      </div>
    </WorkspaceNavContext.Provider>
  );
}

/** The product mark on its own, where the wordmark would not fit. */
export function Mark({
  className = 'size-5',
}: {
  className?: string;
}): React.ReactElement {
  return <BrandMark className={className} />;
}
