/**
 * One private provider: its usage snippet, every version with the platforms it
 * ships for, and the key its checksums were signed with.
 */

import { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  useMutationWithRefetch,
  usePolledQuery,
} from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import { api, type Provider } from '../../api';
import {
  Button,
  CodeBlock,
  Dialog,
  ErrorNotice,
  PageHeader,
  RelativeTime,
  Spinner,
  Table,
  Td,
  Th,
  Tr,
  useIsAdmin,
} from '../../components';
import { registryHost } from './moduleText';
import {
  PROVIDERS_KEY,
  latestPublishedProvider,
  providerKey,
  providerSnippet,
} from './providerText';
import { VersionStatusBadge } from './VersionStatusBadge';

/** The provider page. */
export function ProviderDetail(): React.ReactElement {
  const params = useParams();
  const namespace = params['namespace'] ?? '';
  const type = params['type'] ?? '';
  const auth = useQueryAuth();
  const isAdmin = useIsAdmin();
  const [pollFast, setPollFast] = useState(false);
  const query = usePolledQuery<Provider>(
    async ({ signal }) => {
      const provider = await api.getProvider(namespace, type, { signal });
      setPollFast(provider.versions.some((v) => v.status === 'pending'));
      return provider;
    },
    {
      intervalMs: pollFast ? 5_000 : 30_000,
      queryKey: providerKey(namespace, type),
      auth,
    }
  );
  const provider = query.data;
  const crumbs = [{ label: 'Registry', to: '/registry?tab=providers' }];

  if (provider === null) {
    return (
      <div className="space-y-5">
        <PageHeader title={type} crumbs={crumbs} mono />
        <ErrorNotice error={query.error} />
        {query.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-text-faint">
            <Spinner label="Loading provider" className="size-4" />
            Loading provider
          </div>
        ) : null}
      </div>
    );
  }

  const host = registryHost();
  const latest = latestPublishedProvider(provider.versions);

  return (
    <div className="space-y-5">
      <PageHeader
        title={provider.type}
        crumbs={[...crumbs, { label: provider.namespace }]}
        mono
        description={
          <span className="font-mono text-xs">
            {provider.namespace}/{provider.type}
          </span>
        }
        actions={isAdmin ? <ProviderActions provider={provider} /> : null}
      />
      <ErrorNotice error={query.error} />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0">
          {provider.versions.length === 0 ? (
            <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-text-faint">
              {`No versions yet. Publish a GitHub release with a vX.Y.Z tag on ${provider.vcs_repo}, or resync to import existing releases.`}
            </p>
          ) : (
            <Table label="Versions">
              <thead>
                <tr>
                  <Th>Version</Th>
                  <Th>Status</Th>
                  <Th>Platforms</Th>
                  <Th>Signing key</Th>
                  <Th>Created</Th>
                </tr>
              </thead>
              <tbody>
                {provider.versions.map((version) => (
                  <Tr key={version.version}>
                    <Td className="font-mono text-xs text-text-strong">
                      {version.version}
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
                    <Td className="font-mono text-[11px] text-text-muted">
                      {version.platforms
                        .map((platform) => `${platform.os}_${platform.arch}`)
                        .join(', ')}
                    </Td>
                    <Td className="font-mono text-[11px] text-text-muted">
                      {version.key_id ?? ''}
                    </Td>
                    <Td className="text-xs whitespace-nowrap text-text-faint">
                      <RelativeTime iso={version.created_at} />
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </div>
        <aside className="space-y-4">
          <section className="space-y-2 rounded-lg border border-line bg-panel p-4">
            <h2 className="text-sm font-medium text-text-strong">
              Usage instructions
            </h2>
            <p className="text-xs text-text-muted">
              {
                'Runs here install the provider with their own registry credential. Locally, run '
              }
              <span className="font-mono">terraform login {host}</span>
              {' once, then terraform init verifies the signature on install.'}
            </p>
            <CodeBlock
              subject="provider usage"
              code={providerSnippet(host, provider, latest?.version ?? null)}
            />
          </section>
          <section className="rounded-lg border border-line bg-panel p-4">
            <dl className="space-y-1.5 text-xs">
              <div className="flex justify-between gap-3">
                <dt className="text-text-faint">Repository</dt>
                <dd className="min-w-0 truncate text-right">
                  <a
                    href={`https://github.com/${provider.vcs_repo}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="font-mono text-accent underline-offset-2 hover:underline"
                  >
                    {provider.vcs_repo}
                  </a>
                </dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt className="text-text-faint">Latest</dt>
                <dd className="font-mono text-text">
                  {latest?.version ?? 'None published'}
                </dd>
              </div>
            </dl>
          </section>
        </aside>
      </div>
    </div>
  );
}

/** The resync and delete buttons an admin sees. */
function ProviderActions({
  provider,
}: {
  provider: Provider;
}): React.ReactElement {
  const navigate = useNavigate();
  const [confirming, setConfirming] = useState(false);
  const [queued, setQueued] = useState(false);
  const resync = useMutationWithRefetch(
    () => api.resyncProvider(provider.namespace, provider.type),
    providerKey(provider.namespace, provider.type)
  );
  const remove = useMutationWithRefetch(
    () => api.deleteProvider(provider.namespace, provider.type),
    PROVIDERS_KEY
  );

  return (
    <span className="flex flex-wrap items-center gap-2">
      <Button
        busy={resync.isMutating}
        busyLabel="Queueing resync"
        title="Import every release in the repository that is not published yet"
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
      <Button
        variant="danger"
        onClick={() => {
          setConfirming(true);
        }}
      >
        Delete provider
      </Button>
      {resync.error === null ? null : (
        <ErrorNotice error={resync.error} className="basis-full" />
      )}
      <Dialog
        open={confirming}
        onClose={() => {
          setConfirming(false);
        }}
        title="Delete provider"
        description="Every version and stored artifact is removed, and configurations requiring this provider stop installing. This cannot be undone."
      >
        <div className="space-y-4">
          <p className="text-sm text-text">
            {'Delete '}
            <span className="font-mono text-text-strong">
              {provider.namespace}/{provider.type}
            </span>
            {` and its ${String(provider.versions.length)} version${
              provider.versions.length === 1 ? '' : 's'
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
              busyLabel="Deleting provider"
              onClick={() => {
                void remove
                  .mutate()
                  .then(() => {
                    void navigate('/registry?tab=providers');
                  })
                  .catch(() => undefined);
              }}
            >
              Delete provider
            </Button>
          </div>
        </div>
      </Dialog>
    </span>
  );
}
