/**
 * Connects a provider to a GitHub repository, like publishing a private provider.
 *
 * Each GitHub release with a version tag publishes that version from the
 * GoReleaser registry assets it carries: the zips, `SHA256SUMS`, its detached
 * signature and the manifest. The signature is checked against the registry's
 * signing key before anything is served.
 */

import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutationWithRefetch } from '@webbpulse/api-client/react';

import { api, type ProviderCreate } from '../../api';
import { Button, ErrorNotice, PageHeader, buttonClass } from '../../components';
import { RepositoryPicker } from '../workspace/vcs/RepositoryPicker';
import {
  PROVIDERS_KEY,
  providerPagePath,
  typeFromRepository,
} from './providerText';

/** The connect provider page. */
export function ConnectProvider(): React.ReactElement {
  const navigate = useNavigate();
  const [repository, setRepository] = useState<string | null>(null);
  const [importReleases, setImportReleases] = useState(true);
  const create = useMutationWithRefetch(
    (body: ProviderCreate) => api.createProvider(body),
    PROVIDERS_KEY
  );
  const type = repository === null ? null : typeFromRepository(repository);
  const namespace = repository?.split('/')[0] ?? '';

  const submit = async (): Promise<void> => {
    if (repository === null) {
      return;
    }
    try {
      const provider = await create.mutate({
        vcs_repo: repository,
        import_releases: importReleases,
      });
      void navigate(providerPagePath(provider));
    } catch {
      return;
    }
  };

  return (
    <div className="max-w-3xl space-y-6">
      <PageHeader
        title="Connect a provider"
        crumbs={[{ label: 'Registry', to: '/registry?tab=providers' }]}
        description="Each GitHub release with a vX.Y.Z tag publishes a version, from the GoReleaser registry assets it carries."
      />
      <section className="space-y-3 rounded-lg border border-line bg-panel p-5">
        <h2 className="text-sm font-medium text-text-strong">
          1. Choose a repository
        </h2>
        <p className="text-xs text-text-muted">
          {'The repository must be named '}
          <span className="font-mono">terraform-provider-name</span>
          {' and its releases signed with the registry signing key.'}
        </p>
        <RepositoryPicker
          value={repository}
          onChange={(picked) => {
            setRepository(picked.full_name);
          }}
        />
      </section>
      {repository === null ? null : (
        <section className="space-y-4 rounded-lg border border-line bg-panel p-5">
          <h2 className="text-sm font-medium text-text-strong">
            2. Confirm the provider address
          </h2>
          {type === null ? (
            <p className="text-sm text-danger" role="alert">
              {repository} is not named terraform-provider-name, so it implies
              no provider type.
            </p>
          ) : (
            <p className="text-xs text-text-muted">
              {'Source address '}
              <span
                data-testid="connect-provider-address"
                className="font-mono text-text-strong"
              >
                {namespace}/{type}
              </span>
            </p>
          )}
          <label className="flex items-start gap-2 text-sm text-text">
            <input
              type="checkbox"
              checked={importReleases}
              onChange={(event) => {
                setImportReleases(event.target.checked);
              }}
              className="mt-0.5 accent-accent"
            />
            <span>
              Import existing releases
              <span className="block text-xs text-text-faint">
                Releases already in the repository publish now. Otherwise only
                releases published from here on do, until a resync.
              </span>
            </span>
          </label>
          <ErrorNotice error={create.error} />
          <div className="flex justify-end gap-2">
            <Link to="/registry?tab=providers" className={buttonClass('ghost')}>
              Cancel
            </Link>
            <Button
              variant="primary"
              disabled={type === null}
              busy={create.isMutating}
              busyLabel="Connecting provider"
              onClick={() => {
                void submit();
              }}
            >
              Connect provider
            </Button>
          </div>
        </section>
      )}
    </div>
  );
}
