/** One link in a navigation rail, with an optional icon and an active state. */

import { NavLink } from 'react-router-dom';

/** Props for {@link RailLink}. */
export interface RailLinkProps {
  to: string;
  label: string;
  icon?: React.ReactNode;
  /** Only active on an exact match, for an index link with siblings beneath it. */
  end?: boolean;
  /** Tighter padding, for the horizontal strip on small screens. */
  compact?: boolean;
}

/** A rail link. */
export function RailLink({
  to,
  label,
  icon,
  end = false,
  compact = false,
}: RailLinkProps): React.ReactElement {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        `flex items-center gap-2 rounded-md text-sm whitespace-nowrap transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${
          compact ? 'px-2 py-1' : 'px-2.5 py-1.5'
        } ${
          isActive
            ? 'bg-raised font-medium text-text-strong'
            : 'text-text hover:bg-raised/60 hover:text-text-strong'
        }`
      }
    >
      {icon === undefined ? null : (
        <span className="text-text-faint" aria-hidden="true">
          {icon}
        </span>
      )}
      {label}
    </NavLink>
  );
}

/** The small caps label above a group of rail links. */
export function RailGroupLabel({
  children,
  className = '',
}: {
  children: React.ReactNode;
  className?: string;
}): React.ReactElement {
  return (
    <p
      className={`px-2.5 pt-2 pb-1 text-[11px] font-medium tracking-wide text-text-faint uppercase ${className}`}
    >
      {children}
    </p>
  );
}
