/**
 * One private module, laid out like a public registry module page.
 *
 * The header names the module and picks the version. The body tabs through the
 * readme, inputs, outputs, resources, providers, submodules and every version,
 * and the side column carries the usage snippet and where the version came from.
 */

import { useState } from 'react';
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from 'react-router-dom';
import {
  useMutationWithRefetch,
  usePolledQuery,
} from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  type Module,
  type ModuleDocs,
  type ModuleVersion,
  type ModuleVersionDetail,
} from '../../api';
import {
  Button,
  CodeBlock,
  Dialog,
  ErrorNotice,
  INPUT_CLASS,
  PageHeader,
  RelativeTime,
  Spinner,
  Table,
  Tabs,
  Td,
  Th,
  Tr,
  formatBytes,
  useIsAdmin,
} from '../../components';
import { Markdown } from './Markdown';
import {
  InputsTable,
  OutputsTable,
  ProvidersTable,
  ResourcesTable,
} from './ModuleDocsTables';
import {
  MODULES_KEY,
  hasPending,
  latestPublished,
  moduleKey,
  modulePagePath,
  registryHost,
  sourceAddress,
  tokenVariable,
  usageSnippet,
} from './moduleText';
import { VersionStatusBadge } from './VersionStatusBadge';

type TabId =
  | 'readme'
  | 'inputs'
  | 'outputs'
  | 'resources'
  | 'providers'
  | 'submodules'
  | 'versions';

/** The address in the route, decoded by the router. */
interface Address {
  namespace: string;
  name: string;
  provider: string;
}

/** The module page. */
export function ModuleDetail(): React.ReactElement {
  const params = useParams();
  const address: Address = {
    namespace: params['namespace'] ?? '',
    name: params['name'] ?? '',
    provider: params['provider'] ?? '',
  };
  const auth = useQueryAuth();
  const [search, setSearch] = useSearchParams();
  const key = moduleKey(address.namespace, address.name, address.provider);
  const [pollFast, setPollFast] = useState(false);
  const query = usePolledQuery<Module>(
    async ({ signal }) => {
      const module = await api.getModule(
        address.namespace,
        address.name,
        address.provider,
        { signal }
      );
      setPollFast(hasPending(module.versions));
      return module;
    },
    { intervalMs: pollFast ? 5_000 : 30_000, queryKey: key, auth }
  );
  const module = query.data;
  const crumbs = [{ label: 'Registry', to: '/registry' }];

  if (module === null) {
    return (
      <div className="space-y-5">
        <PageHeader title={address.name} crumbs={crumbs} mono />
        <ErrorNotice error={query.error} />
        {query.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-text-faint">
            <Spinner label="Loading module" className="size-4" />
            Loading module
          </div>
        ) : null}
      </div>
    );
  }

  const requested = search.get('version');
  const selected =
    module.versions.find((version) => version.version === requested) ??
    latestPublished(module.versions) ??
    module.versions[0] ??
    null;

  return (
    <ModulePage
      module={module}
      selected={selected}
      error={query.error}
      onSelect={(version) => {
        setSearch({ version }, { replace: true });
      }}
    />
  );
}

/** The page once the module has loaded. */
function ModulePage({
  module,
  selected,
  error,
  onSelect,
}: {
  module: Module;
  selected: ModuleVersion | null;
  error: unknown;
  onSelect: (version: string) => void;
}): React.ReactElement {
  const auth = useQueryAuth();
  const isAdmin = useIsAdmin();
  const [tab, setTab] = useState<TabId>('readme');
  const published = selected?.status === 'published';
  const detail = usePolledQuery<ModuleVersionDetail>(
    ({ signal }) =>
      api.getModuleVersion(
        module.namespace,
        module.name,
        module.provider,
        selected?.version ?? '',
        { signal }
      ),
    {
      intervalMs: 300_000,
      queryKey: [
        'registry-version',
        module.namespace,
        module.name,
        module.provider,
        selected?.version ?? '',
      ],
      enabled: published,
      auth,
    }
  );
  const docs = published ? (detail.data?.docs ?? null) : null;
  const host = registryHost();
  const source = sourceAddress(host, module);
  const required = (docs?.inputs ?? [])
    .filter((input) => input.required)
    .map((input) => input.name);

  const tabs = [
    { id: 'readme' as const, label: 'Readme' },
    { id: 'inputs' as const, label: 'Inputs', count: docs?.inputs.length },
    { id: 'outputs' as const, label: 'Outputs', count: docs?.outputs.length },
    {
      id: 'resources' as const,
      label: 'Resources',
      count: docs?.resources.length,
    },
    {
      id: 'providers' as const,
      label: 'Providers',
      count: docs?.providers.length,
    },
    {
      id: 'submodules' as const,
      label: 'Submodules',
      count: docs?.submodules.length,
    },
    {
      id: 'versions' as const,
      label: 'Versions',
      count: module.versions.length,
    },
  ].map(({ count, ...rest }) =>
    count === undefined ? rest : { ...rest, count }
  );

  return (
    <div className="space-y-5">
      <PageHeader
        title={module.name}
        crumbs={[
          { label: 'Registry', to: '/registry' },
          { label: module.namespace },
        ]}
        mono
        description={
          <span className="font-mono text-xs">
            {module.namespace}/{module.name}/{module.provider}
          </span>
        }
        meta={
          <span className="flex items-center gap-2">
            <span className="rounded border border-line bg-raised px-1.5 py-0.5 font-mono text-[11px] text-text-muted">
              {module.provider}
            </span>
            {selected === null ? null : (
              <VersionStatusBadge status={selected.status} />
            )}
          </span>
        }
        actions={
          <span className="flex items-center gap-2">
            {module.versions.length === 0 ? null : (
              <select
                aria-label="Version"
                value={selected?.version ?? ''}
                onChange={(event) => {
                  onSelect(event.target.value);
                }}
                className={`${INPUT_CLASS} mt-0 w-auto font-mono`}
              >
                {module.versions.map((version) => (
                  <option key={version.version} value={version.version}>
                    {version.version}
                    {version.status === 'published'
                      ? ''
                      : ` (${version.status})`}
                  </option>
                ))}
              </select>
            )}
            {isAdmin ? <ModuleActions module={module} /> : null}
          </span>
        }
      />
      <ErrorNotice error={error} />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-4">
          <Tabs tabs={tabs} value={tab} onChange={setTab} label="Module" />
          <div role="tabpanel">
            {tab === 'versions' ? (
              <VersionsTable
                module={module}
                selected={selected}
                onSelect={onSelect}
              />
            ) : (
              <DocsPanel
                tab={tab}
                module={module}
                selected={selected}
                docs={docs}
                loading={published && detail.isLoading}
                error={detail.error}
                host={host}
              />
            )}
          </div>
        </div>
        <aside className="space-y-4">
          <section className="space-y-2 rounded-lg border border-line bg-panel p-4">
            <h2 className="text-sm font-medium text-text-strong">
              Usage instructions
            </h2>
            <p className="text-xs text-text-muted">
              Runs here fetch the module with their own registry credential. A
              local init needs an API key with registry read access in{' '}
              <span className="font-mono">{tokenVariable(host)}</span>.
            </p>
            <CodeBlock
              subject="module usage"
              code={usageSnippet(
                source,
                module.name,
                published ? (selected?.version ?? null) : null,
                required
              )}
            />
          </section>
          {selected === null ? null : (
            <VersionFacts module={module} version={selected} />
          )}
        </aside>
      </div>
    </div>
  );
}

/** The resync and delete buttons an admin sees. */
function ModuleActions({ module }: { module: Module }): React.ReactElement {
  const navigate = useNavigate();
  const [confirming, setConfirming] = useState(false);
  const [queued, setQueued] = useState(false);
  const key = moduleKey(module.namespace, module.name, module.provider);
  const resync = useMutationWithRefetch(
    () => api.resyncModule(module.namespace, module.name, module.provider),
    key
  );
  const remove = useMutationWithRefetch(
    () => api.deleteModule(module.namespace, module.name, module.provider),
    MODULES_KEY
  );

  return (
    <>
      {module.vcs_repo === null || module.vcs_repo === undefined ? null : (
        <Button
          busy={resync.isMutating}
          busyLabel="Queueing resync"
          title="Import every version tag in the repository that is not published yet"
          onClick={() => {
            void resync
              .mutate()
              .then(() => {
                setQueued(true);
              })
              .catch(() => undefined);
          }}
        >
          {queued ? 'Resync queued' : 'Resync'}
        </Button>
      )}
      <Button
        variant="danger"
        onClick={() => {
          setConfirming(true);
        }}
      >
        Delete module
      </Button>
      {resync.error === null ? null : (
        <ErrorNotice error={resync.error} className="basis-full" />
      )}
      <Dialog
        open={confirming}
        onClose={() => {
          setConfirming(false);
        }}
        title="Delete module"
        description="Every version and stored tarball is removed, and configurations pinned to this module stop resolving. This cannot be undone."
      >
        <div className="space-y-4">
          <p className="text-sm text-text">
            {'Delete '}
            <span className="font-mono text-text-strong">
              {module.namespace}/{module.name}/{module.provider}
            </span>
            {` and its ${String(module.versions.length)} version${
              module.versions.length === 1 ? '' : 's'
            }?`}
          </p>
          <ErrorNotice error={remove.error} />
          <div className="flex justify-end gap-2">
            <Button
              variant="ghost"
              onClick={() => {
                setConfirming(false);
              }}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              busy={remove.isMutating}
              busyLabel="Deleting module"
              onClick={() => {
                void remove
                  .mutate()
                  .then(() => {
                    void navigate('/registry');
                  })
                  .catch(() => undefined);
              }}
            >
              Delete module
            </Button>
          </div>
        </div>
      </Dialog>
    </>
  );
}

/** Where the selected version came from and when it was published. */
function VersionFacts({
  module,
  version,
}: {
  module: Module;
  version: ModuleVersion;
}): React.ReactElement {
  const repository = module.vcs_repo ?? version.repository;
  const facts: { label: string; value: React.ReactNode }[] = [
    {
      label: 'Repository',
      value: (
        <a
          href={`https://github.com/${repository}`}
          target="_blank"
          rel="noopener noreferrer"
          className="font-mono text-accent underline-offset-2 hover:underline"
        >
          {repository}
        </a>
      ),
    },
    {
      label: 'Tag',
      value: (
        <span className="font-mono">{version.tag ?? version.version}</span>
      ),
    },
    {
      label: 'Commit',
      value: <span className="font-mono">{version.sha.slice(0, 7)}</span>,
    },
    {
      label: 'Published',
      value: version.published_at ? (
        <RelativeTime iso={version.published_at} />
      ) : (
        'Not yet'
      ),
    },
  ];
  if (version.size_bytes !== null && version.size_bytes !== undefined) {
    facts.push({ label: 'Size', value: formatBytes(version.size_bytes) });
  }
  return (
    <section className="rounded-lg border border-line bg-panel p-4">
      <h2 className="mb-2 text-sm font-medium text-text-strong">
        Version {version.version}
      </h2>
      <dl className="space-y-1.5 text-xs">
        {facts.map((fact) => (
          <div key={fact.label} className="flex justify-between gap-3">
            <dt className="text-text-faint">{fact.label}</dt>
            <dd className="min-w-0 truncate text-right text-text">
              {fact.value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** One documentation tab, or why the selected version has none to show. */
function DocsPanel({
  tab,
  module,
  selected,
  docs,
  loading,
  error,
  host,
}: {
  tab: Exclude<TabId, 'versions'>;
  module: Module;
  selected: ModuleVersion | null;
  docs: ModuleDocs | null;
  loading: boolean;
  error: unknown;
  host: string;
}): React.ReactElement {
  if (selected === null) {
    return (
      <Notice>
        No version is published yet.
        {module.vcs_repo
          ? ` Push a vX.Y.Z tag to ${module.vcs_repo}, or resync to import existing tags.`
          : ''}
      </Notice>
    );
  }
  if (selected.status === 'pending') {
    return (
      <Notice>
        <span className="inline-flex items-center gap-2">
          <Spinner label="Publishing" className="size-3.5" />
          Version {selected.version} is being published.
        </span>
      </Notice>
    );
  }
  if (selected.status === 'failed') {
    return (
      <div className="space-y-2 rounded-lg border border-danger-line bg-danger-soft p-4 text-sm">
        <p className="font-medium text-danger">
          Version {selected.version} failed to publish.
        </p>
        {selected.error ? (
          <p className="font-mono text-xs text-text">{selected.error}</p>
        ) : null}
      </div>
    );
  }
  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-text-faint">
        <Spinner label="Loading documentation" className="size-4" />
        Loading documentation
      </div>
    );
  }
  if (docs === null) {
    return (
      <div className="space-y-3">
        <ErrorNotice error={error} />
        <Notice>No documentation could be read from this version.</Notice>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {docs.parse_errors.length === 0 ? null : (
        <p className="rounded-md border border-warning-line bg-warning-soft px-3 py-2 text-xs text-warning">
          {'Some files could not be read and are left out: '}
          <span className="font-mono">{docs.parse_errors.join(', ')}</span>
        </p>
      )}
      {tab === 'readme' ? (
        docs.readme ? (
          <section className="rounded-lg border border-line bg-panel p-5">
            <Markdown source={docs.readme} />
          </section>
        ) : (
          <Notice>This version has no readme.</Notice>
        )
      ) : null}
      {tab === 'inputs' ? <InputsTable inputs={docs.inputs} /> : null}
      {tab === 'outputs' ? <OutputsTable outputs={docs.outputs} /> : null}
      {tab === 'resources' ? (
        <ResourcesTable resources={docs.resources} />
      ) : null}
      {tab === 'providers' ? (
        <ProvidersTable providers={docs.providers} />
      ) : null}
      {tab === 'submodules' ? (
        <Submodules
          module={module}
          version={selected.version}
          docs={docs}
          host={host}
        />
      ) : null}
    </div>
  );
}

/** Each submodule with its own usage snippet, inputs and outputs. */
function Submodules({
  module,
  version,
  docs,
  host,
}: {
  module: Module;
  version: string;
  docs: ModuleDocs;
  host: string;
}): React.ReactElement {
  if (docs.submodules.length === 0) {
    return <Notice>This version has no submodules under modules/.</Notice>;
  }
  return (
    <div className="space-y-4">
      {docs.submodules.map((submodule) => (
        <section
          key={submodule.path}
          aria-label={`Submodule ${submodule.name}`}
          className="space-y-3 rounded-lg border border-line bg-panel p-4"
        >
          <h3 className="font-mono text-sm font-medium text-text-strong">
            {submodule.path}
          </h3>
          <CodeBlock
            subject={`${submodule.name} usage`}
            code={usageSnippet(
              sourceAddress(host, module, submodule.name),
              submodule.name,
              version,
              submodule.inputs
                .filter((input) => input.required)
                .map((input) => input.name)
            )}
          />
          <InputsTable inputs={submodule.inputs} />
          <OutputsTable outputs={submodule.outputs} />
        </section>
      ))}
    </div>
  );
}

/** Every version, newest first, with its status and any publishing error. */
function VersionsTable({
  module,
  selected,
  onSelect,
}: {
  module: Module;
  selected: ModuleVersion | null;
  onSelect: (version: string) => void;
}): React.ReactElement {
  if (module.versions.length === 0) {
    return <Notice>No versions yet.</Notice>;
  }
  return (
    <Table label="Versions">
      <thead>
        <tr>
          <Th>Version</Th>
          <Th>Status</Th>
          <Th>Commit</Th>
          <Th>Created</Th>
        </tr>
      </thead>
      <tbody>
        {module.versions.map((version) => (
          <Tr key={version.version}>
            <Td>
              <Link
                to={modulePagePath(module, version.version)}
                onClick={(event) => {
                  event.preventDefault();
                  onSelect(version.version);
                }}
                aria-current={
                  version.version === selected?.version ? 'true' : undefined
                }
                className="font-mono text-xs text-text-strong hover:text-accent hover:underline"
              >
                {version.version}
              </Link>
            </Td>
            <Td>
              <span className="flex flex-col items-start gap-1">
                <VersionStatusBadge status={version.status} />
                {version.status === 'failed' && version.error ? (
                  <span className="max-w-md font-mono text-[11px] text-danger">
                    {version.error}
                  </span>
                ) : null}
              </span>
            </Td>
            <Td className="font-mono text-xs text-text-muted">
              {version.sha.slice(0, 7)}
            </Td>
            <Td className="text-xs whitespace-nowrap text-text-faint">
              <RelativeTime iso={version.created_at} />
            </Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}

/** A quiet line in place of a panel's content. */
function Notice({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
      {children}
    </p>
  );
}
