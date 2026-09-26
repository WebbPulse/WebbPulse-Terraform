/** The repositories this environment's GitHub App can see, for picking one to connect. */

import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import { api, type Repository } from '../../../api';

/** One repository, with the account whose installation covers it. */
export interface InstalledRepository extends Repository {
  account_login: string;
}

/** Whether the App exists and, when it does, every repository its installations reach. */
export interface InstalledRepositories {
  configured: boolean;
  installations: number;
  repositories: InstalledRepository[];
}

/** The query key the picker reads under. */
export const INSTALLED_REPOSITORIES_KEY = 'github-installed-repositories';

/** Reads the App, its installations and their repositories in one pass, sorted by name. */
export async function loadInstalledRepositories(
  signal: AbortSignal
): Promise<InstalledRepositories> {
  const app = await api.getGitHubApp({ signal });
  if (!app.configured) {
    return { configured: false, installations: 0, repositories: [] };
  }
  const installations = (
    await api.listGitHubInstallations({ signal })
  ).items.filter((installation) => !installation.suspended);
  const lists = await Promise.all(
    installations.map(async (installation) => {
      const list = await api.listGitHubRepositories(
        installation.installation_id,
        { signal }
      );
      return list.items.map((repository) => ({
        ...repository,
        account_login: installation.account_login,
      }));
    })
  );
  const repositories = lists
    .flat()
    .sort((a, b) => a.full_name.localeCompare(b.full_name));
  return {
    configured: true,
    installations: installations.length,
    repositories,
  };
}

/**
 * The installed repositories, read only for admins.
 *
 * The GitHub routes carry the admin scope, so anyone else would only ever see
 * a 403; `enabled` keeps the query from firing for them at all.
 */
export function useInstalledRepositories(enabled: boolean): {
  data: InstalledRepositories | null;
  error: unknown;
  isLoading: boolean;
} {
  const auth = useQueryAuth();
  const query = usePolledQuery<InstalledRepositories>(
    ({ signal }) => loadInstalledRepositories(signal),
    {
      intervalMs: 300_000,
      queryKey: INSTALLED_REPOSITORIES_KEY,
      auth,
      enabled,
    }
  );
  return {
    data: query.data,
    error: query.error,
    isLoading: enabled && query.isLoading,
  };
}
