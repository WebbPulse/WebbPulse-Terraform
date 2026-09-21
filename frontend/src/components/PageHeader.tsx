/** The header every page opens with: a title, one line under it, and actions. */

import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';

/** One crumb of the trail above the title. */
export interface Crumb {
  label: string;
  to?: string;
}

/** Props for {@link PageHeader}. */
export interface PageHeaderProps {
  title: ReactNode;
  /** The trail above the title, the current page left off. */
  crumbs?: readonly Crumb[];
  /** One line under the title. */
  description?: ReactNode;
  /** Small things beside the title, such as a status pill. */
  meta?: ReactNode;
  /** Buttons on the right. */
  actions?: ReactNode;
  /** Whether the title is an identifier and reads better in monospace. */
  mono?: boolean;
}

/** The page header. */
export function PageHeader({
  title,
  crumbs = [],
  description,
  meta,
  actions,
  mono = false,
}: PageHeaderProps): React.ReactElement {
  return (
    <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
      <div className="min-w-0">
        {crumbs.length === 0 ? null : (
          <nav aria-label="Breadcrumb" className="mb-1 text-xs text-text-faint">
            <ol className="flex items-center gap-1.5">
              {crumbs.map((crumb) => (
                <li key={crumb.label} className="flex items-center gap-1.5">
                  {crumb.to === undefined ? (
                    <span>{crumb.label}</span>
                  ) : (
                    <Link
                      to={crumb.to}
                      className="rounded hover:text-accent hover:underline"
                    >
                      {crumb.label}
                    </Link>
                  )}
                  <span aria-hidden="true" className="text-line-strong">
                    /
                  </span>
                </li>
              ))}
            </ol>
          </nav>
        )}
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <h1
            className={`truncate text-lg font-semibold tracking-tight text-text-strong ${
              mono ? 'font-mono text-base' : ''
            }`}
          >
            {title}
          </h1>
          {meta}
        </div>
        {description === undefined ? null : (
          <p className="mt-1 max-w-prose text-sm text-text-muted">
            {description}
          </p>
        )}
      </div>
      {actions === undefined ? null : (
        <div className="flex shrink-0 items-center gap-2">{actions}</div>
      )}
    </header>
  );
}
