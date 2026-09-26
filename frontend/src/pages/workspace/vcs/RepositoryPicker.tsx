/** Picks one of the repositories the GitHub App is installed on. */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { ErrorNotice, INPUT_CLASS, useIsAdmin } from '../../../components';
import {
  useInstalledRepositories,
  type InstalledRepositories,
  type InstalledRepository,
} from './useInstalledRepositories';
import { sameRepository } from './vcsSettings';

/** Props for {@link RepositoryPicker}. */
export interface RepositoryPickerProps {
  /** The full name currently picked, or null. */
  value: string | null;
  onChange: (repository: InstalledRepository) => void;
}

/** A filterable list of installed repositories, or the way to install the App. */
export function RepositoryPicker({
  value,
  onChange,
}: RepositoryPickerProps): React.ReactElement {
  const isAdmin = useIsAdmin();
  const source = useInstalledRepositories(isAdmin);
  return (
    <RepositoryList
      isAdmin={isAdmin}
      source={source}
      value={value}
      onChange={onChange}
    />
  );
}

/** Props for {@link RepositoryList}. */
export interface RepositoryListProps extends RepositoryPickerProps {
  isAdmin: boolean;
  /** The installed repositories, read by the caller so it can start the read early. */
  source: {
    data: InstalledRepositories | null;
    error: unknown;
    isLoading: boolean;
  };
}

/** The number of placeholder rows shown while the repositories load. */
const SKELETON_ROWS = 5;

/** Placeholder rows shaped like the list, labelled for assistive technology. */
function RepositorySkeleton(): React.ReactElement {
  return (
    <div className="space-y-2">
      <p className="text-xs text-text-faint">
        Loading repositories from GitHub
      </p>
      <ul
        role="status"
        aria-label="Loading repositories"
        aria-busy="true"
        className="overflow-hidden rounded-md border border-line bg-panel"
      >
        {Array.from({ length: SKELETON_ROWS }, (_, index) => (
          <li
            key={index}
            aria-hidden="true"
            className="flex items-center justify-between gap-3 border-b border-line px-3 py-2.5 last:border-b-0"
          >
            <span className="space-y-1.5">
              <span
                className="block h-3 animate-pulse rounded bg-raised"
                style={{ width: `${String(10 + ((index * 3) % 7))}rem` }}
              />
              <span className="block h-2.5 w-24 animate-pulse rounded bg-raised" />
            </span>
            <span className="h-4 w-12 animate-pulse rounded bg-raised" />
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The chip that says whether a repository is public or private. */
function VisibilityChip({
  isPrivate,
}: {
  isPrivate: boolean;
}): React.ReactElement {
  return (
    <span className="shrink-0 rounded border border-line bg-raised px-1.5 py-0.5 text-[11px] font-medium text-text-muted">
      {isPrivate ? 'Private' : 'Public'}
    </span>
  );
}

/** The list itself, given the repositories to show. */
export function RepositoryList({
  isAdmin,
  source,
  value,
  onChange,
}: RepositoryListProps): React.ReactElement {
  const [filter, setFilter] = useState('');
  const { data, error, isLoading } = source;

  if (!isAdmin) {
    return (
      <p className="rounded-md border border-line bg-raised p-3 text-sm text-text-muted">
        Only an admin can connect a repository, since the repositories come from
        the organization's GitHub App.
      </p>
    );
  }
  if (isLoading) {
    return <RepositorySkeleton />;
  }
  if (data === null) {
    return <ErrorNotice error={error} />;
  }
  if (!data.configured || data.installations === 0) {
    return (
      <div className="rounded-md border border-line bg-raised p-3 text-sm text-text-muted">
        {data.configured
          ? 'The GitHub App is not installed on any account yet. '
          : 'This environment has no GitHub App yet. '}
        <Link
          to="/settings/github"
          className="text-accent underline-offset-2 hover:underline"
        >
          {data.configured ? 'Install the GitHub App' : 'Set up the GitHub App'}
        </Link>
        , then pick a repository here.
      </div>
    );
  }

  const needle = filter.trim().toLowerCase();
  const shown = data.repositories.filter((repository) =>
    repository.full_name.toLowerCase().includes(needle)
  );

  return (
    <div className="space-y-2">
      <input
        type="search"
        aria-label="Filter repositories"
        placeholder="Filter repositories"
        value={filter}
        onChange={(event) => {
          setFilter(event.target.value);
        }}
        className={INPUT_CLASS}
      />
      <ul
        role="listbox"
        aria-label="Repositories"
        className="max-h-80 overflow-y-auto rounded-md border border-line bg-panel"
      >
        {shown.length === 0 ? (
          <li className="px-3 py-2 text-sm text-text-muted">
            No repository matches.
          </li>
        ) : (
          shown.map((repository) => {
            const selected = sameRepository(repository.full_name, value);
            return (
              <li
                key={repository.id}
                role="option"
                aria-selected={selected}
                tabIndex={0}
                onClick={() => {
                  onChange(repository);
                }}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    onChange(repository);
                  }
                }}
                className={`flex cursor-pointer items-center justify-between gap-3 border-b border-line px-3 py-2 text-sm last:border-b-0 hover:bg-raised focus-visible:ring-1 focus-visible:ring-accent focus-visible:outline-none ${
                  selected ? 'bg-raised text-text-strong' : 'text-text'
                }`}
              >
                <span className="min-w-0">
                  <span className="block truncate font-mono">
                    {repository.full_name}
                  </span>
                  {repository.default_branch ? (
                    <span className="block text-xs text-text-faint">
                      default branch{' '}
                      <span className="font-mono">
                        {repository.default_branch}
                      </span>
                    </span>
                  ) : null}
                </span>
                <VisibilityChip isPrivate={repository.private} />
              </li>
            );
          })
        )}
      </ul>
      <p className="text-xs text-text-muted">
        Missing a repository?{' '}
        <Link
          to="/settings/github"
          className="text-accent underline-offset-2 hover:underline"
        >
          Manage the GitHub App
        </Link>
        .
      </p>
    </div>
  );
}
