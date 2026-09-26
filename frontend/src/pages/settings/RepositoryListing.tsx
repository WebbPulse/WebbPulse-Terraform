/** The repositories one installation reaches, as a read only list. */

import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import { api, type RepositoryList } from '../../api';
import { ErrorNotice } from '../../components';
import { RowsSkeleton } from './Skeleton';

/** The repositories one installation reaches, read only. */
export function RepositoryListing({
  installationId,
}: {
  installationId: string;
}): React.ReactElement {
  const auth = useQueryAuth();
  const query = usePolledQuery<RepositoryList>(
    ({ signal }) => api.listGitHubRepositories(installationId, { signal }),
    {
      intervalMs: 300_000,
      queryKey: `github-repositories-${installationId}`,
      auth,
    }
  );
  if (query.isLoading) {
    return <RowsSkeleton label="Loading repositories" rows={2} />;
  }
  if (query.error !== null && query.error !== undefined) {
    return <ErrorNotice error={query.error} />;
  }
  const items = query.data?.items ?? [];
  if (items.length === 0) {
    return (
      <p className="text-sm text-text-faint">
        This installation reaches no repositories.
      </p>
    );
  }
  return (
    <ul
      aria-label={`Repositories for installation ${installationId}`}
      className="divide-y divide-line rounded-md border border-line"
    >
      {items.map((repo) => (
        <li
          key={repo.id}
          className="flex items-center justify-between gap-3 px-3 py-2 text-sm"
        >
          <a
            href={repo.html_url ?? `https://github.com/${repo.full_name}`}
            target="_blank"
            rel="noreferrer"
            className="truncate font-mono text-text hover:text-accent hover:underline"
          >
            {repo.full_name}
          </a>
          <span className="shrink-0 text-xs text-text-faint">
            {repo.private ? 'Private' : 'Public'}
            {(repo.default_branch ?? '') === ''
              ? ''
              : ` · ${repo.default_branch ?? ''}`}
          </span>
        </li>
      ))}
    </ul>
  );
}
