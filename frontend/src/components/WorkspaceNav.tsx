/** The rail a workspace swaps in: its sections, and its settings pages once inside them. */

import { Link, useMatch } from 'react-router-dom';

import { RailGroupLabel, RailLink } from './RailLink';

/** Props for {@link WorkspaceNav}. */
export interface WorkspaceNavProps {
  workspaceId: string;
  /** The workspace name, or null while it loads. */
  name: string | null;
  /** A horizontal strip rather than a column. */
  compact?: boolean;
}

/** The sections of a workspace, in rail order. */
const SECTIONS: readonly { path: string; label: string; end?: boolean }[] = [
  { path: '', label: 'Overview', end: true },
  { path: 'runs', label: 'Runs' },
  { path: 'configuration-versions', label: 'Configuration versions' },
  { path: 'variables', label: 'Variables' },
  { path: 'settings', label: 'Settings' },
];

/** The settings pages, in rail order. */
const SETTINGS: readonly { path: string; label: string }[] = [
  { path: 'general', label: 'General' },
  { path: 'run-role', label: 'AWS account' },
  { path: 'deletion', label: 'Destruction and deletion' },
];

/** The workspace rail, or the settings rail while a settings page is open. */
export function WorkspaceNav({
  workspaceId,
  name,
  compact = false,
}: WorkspaceNavProps): React.ReactElement {
  const base = `/workspaces/${workspaceId}`;
  const inSettings = useMatch(`${base}/settings/*`) !== null;
  const items = inSettings
    ? SETTINGS.map((item) => ({
        to: `${base}/settings/${item.path}`,
        label: item.label,
        end: false,
      }))
    : SECTIONS.map((item) => ({
        to: item.path === '' ? base : `${base}/${item.path}`,
        label: item.label,
        end: item.end ?? false,
      }));
  const back = inSettings
    ? { to: base, label: name ?? 'Workspace' }
    : { to: '/workspaces', label: 'Workspaces' };
  const heading = inSettings ? 'Workspace settings' : (name ?? 'Workspace');

  return (
    <nav
      aria-label="Workspace sections"
      className={
        compact
          ? 'flex items-center gap-1 overflow-x-auto'
          : 'flex-1 space-y-0.5 p-2'
      }
    >
      <Link
        to={back.to}
        className={`flex items-center gap-1 rounded-md text-xs text-text-muted hover:text-text-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
          compact ? 'px-2 py-1' : 'px-2.5 py-1.5'
        }`}
      >
        <svg viewBox="0 0 16 16" className="size-3" fill="none" aria-hidden>
          <path
            d="M10 3 5 8l5 5"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        {back.label}
      </Link>
      {compact ? null : (
        <RailGroupLabel className="truncate normal-case">
          {heading}
        </RailGroupLabel>
      )}
      {items.map((item) => (
        <RailLink
          key={item.to}
          to={item.to}
          label={item.label}
          end={item.end}
          compact={compact}
        />
      ))}
    </nav>
  );
}
