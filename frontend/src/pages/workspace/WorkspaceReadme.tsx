/** The workspace README on the overview, as HCP Terraform shows it. */

import { useMemo } from 'react';

import { useQueryAuth } from '@webbpulse/auth/react';
import { usePolledQuery } from '@webbpulse/api-client/react';

import { api, type ConfigVersion, type ConfigVersionDetail } from '../../api';
import { Markdown } from '../../components';
import { readmeResolvers, readmeSourceUrl, readmeVersion } from './readme';

/** Props for {@link WorkspaceReadme}. */
export interface WorkspaceReadmeProps {
  workspaceId: string;
  versions: readonly ConfigVersion[];
  workingDirectory: string;
}

/**
 * The README of the configuration the workspace tracks.
 *
 * The README is stored on the configuration version when it is ingested, so this
 * reads one row and never reaches GitHub. Nothing renders until it arrives, and
 * a version with no README leaves a one line hint rather than an empty card.
 */
export function WorkspaceReadme({
  workspaceId,
  versions,
  workingDirectory,
}: WorkspaceReadmeProps): React.ReactElement | null {
  const auth = useQueryAuth();
  const version = readmeVersion(versions);
  const versionId = version?.config_version_id ?? '';
  const query = usePolledQuery<ConfigVersionDetail>(
    ({ signal }) => api.getConfigVersion(workspaceId, versionId, { signal }),
    {
      intervalMs: 300_000,
      queryKey: ['config-version-readme', workspaceId, versionId],
      enabled: versionId !== '',
      auth,
    }
  );
  const detail =
    query.data?.config_version_id === versionId && versionId !== ''
      ? query.data
      : null;
  const readme = detail?.readme ?? null;
  const resolvers = useMemo(
    () => readmeResolvers(detail?.vcs, readme?.path ?? ''),
    [detail?.vcs, readme?.path]
  );

  if (detail === null) {
    return null;
  }
  if (readme === null) {
    const where = workingDirectory.replace(/^\.?\/+|\/+$/g, '');
    return (
      <p
        data-testid="workspace-readme-hint"
        className="text-xs text-text-faint"
      >
        Add a README.md to{' '}
        {where === '' ? (
          'the root of the configuration'
        ) : (
          <span className="font-mono">{where}</span>
        )}{' '}
        to describe this workspace here.
      </p>
    );
  }
  const source = readmeSourceUrl(detail.vcs, readme.path);
  return (
    <section
      aria-labelledby="workspace-readme"
      data-testid="workspace-readme-card"
      className="rounded-lg border border-line bg-panel"
    >
      <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
        <h2
          id="workspace-readme"
          className="truncate font-mono text-xs font-medium text-text-strong"
        >
          {readme.path}
        </h2>
        {source === null ? null : (
          <a
            href={source}
            target="_blank"
            rel="noopener noreferrer"
            className="shrink-0 text-xs text-accent hover:text-accent-hover hover:underline"
          >
            View on GitHub
          </a>
        )}
      </header>
      <div className="px-5 py-4">
        <Markdown
          source={readme.content}
          testId="workspace-readme"
          resolveHref={resolvers.resolveHref}
          resolveSrc={resolvers.resolveSrc}
        />
        {readme.truncated ? (
          <p
            data-testid="workspace-readme-truncated"
            className="mt-4 border-t border-line pt-3 text-xs text-text-faint"
          >
            This README is too long to show in full.
            {source === null ? null : (
              <>
                {' '}
                <a
                  href={source}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-accent hover:underline"
                >
                  Read the rest on GitHub
                </a>
                .
              </>
            )}
          </p>
        ) : null}
      </div>
    </section>
  );
}
