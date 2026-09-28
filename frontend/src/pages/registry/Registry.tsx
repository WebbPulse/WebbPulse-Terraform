/** The private registry's module list, with the way to connect a module. */

import { useState } from 'react';
import { Link } from 'react-router-dom';
import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import { api, type Module, type ModuleList } from '../../api';
import {
  EmptyState,
  ErrorNotice,
  INPUT_CLASS,
  PageHeader,
  RelativeTime,
  Spinner,
  Table,
  Td,
  Th,
  Tr,
  buttonClass,
  useIsAdmin,
} from '../../components';
import { MODULES_KEY, latestPublished, modulePagePath } from './moduleText';
import { VersionStatusBadge } from './VersionStatusBadge';

/** The module list. */
export function Registry(): React.ReactElement {
  const auth = useQueryAuth();
  const isAdmin = useIsAdmin();
  const [filter, setFilter] = useState('');
  const query = usePolledQuery<ModuleList>(
    ({ signal }) => api.listModules({ signal }),
    { intervalMs: 30_000, queryKey: MODULES_KEY, auth }
  );
  const connect = isAdmin ? (
    <Link to="/registry/new" className={buttonClass('primary')}>
      Connect module
    </Link>
  ) : null;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Registry"
        description="Private modules published from GitHub tags, for any workspace to source."
        meta={
          query.isFetching && !query.isLoading ? (
            <Spinner label="Refreshing modules" className="size-3.5" />
          ) : null
        }
        actions={connect}
      />
      <ErrorNotice error={query.error} />
      {query.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Loading modules" className="size-4" />
          Loading modules
        </div>
      ) : query.data === null ? null : (
        <ModuleTable
          modules={query.data.modules}
          filter={filter}
          onFilter={setFilter}
          connect={connect}
        />
      )}
    </div>
  );
}

/** The table of modules behind a filter, or an invitation to connect the first one. */
function ModuleTable({
  modules,
  filter,
  onFilter,
  connect,
}: {
  modules: Module[];
  filter: string;
  onFilter: (value: string) => void;
  connect: React.ReactNode;
}): React.ReactElement {
  if (modules.length === 0) {
    return (
      <EmptyState
        title="No modules yet."
        hint="Connect a repository the GitHub App can reach. Each vX.Y.Z tag pushed there publishes a version."
        action={connect}
      />
    );
  }
  const needle = filter.trim().toLowerCase();
  const shown =
    needle === ''
      ? modules
      : modules.filter((module) =>
          `${module.namespace}/${module.name}/${module.provider}`
            .toLowerCase()
            .includes(needle)
        );
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <input
          type="search"
          aria-label="Filter modules"
          placeholder="Filter modules"
          value={filter}
          onChange={(event) => {
            onFilter(event.target.value);
          }}
          className={`${INPUT_CLASS} max-w-xs`}
        />
        <span className="text-xs text-text-faint">
          {shown.length} of {modules.length}
        </span>
      </div>
      {shown.length === 0 ? (
        <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
          No modules match that filter.
        </p>
      ) : (
        <Table label="Modules">
          <thead>
            <tr>
              <Th>Module</Th>
              <Th>Provider</Th>
              <Th>Latest version</Th>
              <Th>Repository</Th>
              <Th>Published</Th>
            </tr>
          </thead>
          <tbody>
            {shown.map((module) => (
              <ModuleRow key={module.source} module={module} />
            ))}
          </tbody>
        </Table>
      )}
    </div>
  );
}

/** One module: its name, provider, newest version and repository. */
function ModuleRow({ module }: { module: Module }): React.ReactElement {
  const latest = latestPublished(module.versions);
  const newest = module.versions[0] ?? null;
  return (
    <Tr>
      <Td>
        <Link
          to={modulePagePath(module)}
          className="font-medium text-text-strong hover:text-accent hover:underline"
        >
          {module.name}
        </Link>
        <p className="font-mono text-xs text-text-faint">{module.namespace}</p>
      </Td>
      <Td>
        <span className="rounded border border-line bg-raised px-1.5 py-0.5 font-mono text-[11px] text-text-muted">
          {module.provider}
        </span>
      </Td>
      <Td>
        <span className="inline-flex items-center gap-2">
          {latest === null ? (
            <span className="text-xs text-text-faint">None published</span>
          ) : (
            <span className="font-mono text-xs text-text">
              {latest.version}
            </span>
          )}
          {newest !== null && newest.status !== 'published' ? (
            <VersionStatusBadge status={newest.status} />
          ) : null}
        </span>
      </Td>
      <Td className="font-mono text-xs text-text-muted">
        {module.vcs_repo ?? <span className="font-sans">Not connected</span>}
      </Td>
      <Td className="text-xs whitespace-nowrap text-text-faint">
        {latest?.published_at ? (
          <RelativeTime iso={latest.published_at} />
        ) : null}
      </Td>
    </Tr>
  );
}
