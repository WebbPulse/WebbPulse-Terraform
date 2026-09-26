/** Picks one of the repositories the GitHub App is installed on. */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import {
  ErrorNotice,
  INPUT_CLASS,
  Spinner,
  useIsAdmin,
} from '../../../components';
import {
  useInstalledRepositories,
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
  const { data, error, isLoading } = useInstalledRepositories(isAdmin);
  const [filter, setFilter] = useState('');

  if (!isAdmin) {
    return (
      <p className="rounded-md border border-line bg-raised p-3 text-sm text-text-muted">
        Only an admin can connect a repository, since the repositories come from
        the organization's GitHub App.
      </p>
    );
  }
  if (isLoading) {
    return <Spinner label="Loading repositories" />;
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
        className="max-h-64 overflow-y-auto rounded-md border border-line bg-panel"
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
                className={`flex cursor-pointer items-center justify-between gap-3 border-b border-line px-3 py-2 text-sm last:border-b-0 hover:bg-raised focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent ${
                  selected ? 'bg-raised text-text-strong' : 'text-text'
                }`}
              >
                <span className="font-mono">{repository.full_name}</span>
                <span className="text-xs text-text-muted">
                  {repository.private ? 'Private' : 'Public'}
                  {repository.default_branch
                    ? ` · ${repository.default_branch}`
                    : ''}
                </span>
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
