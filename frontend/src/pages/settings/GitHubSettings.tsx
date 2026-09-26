/** Global settings for GitHub: create the App, install it and see what it reaches. Admin only. */

import { useState } from 'react';
import {
  useMutationWithRefetch,
  usePolledQuery,
} from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  type GitHubAppStatus,
  type Installation,
  type InstallationList,
  type RepositoryList,
} from '../../api';
import {
  Button,
  Dialog,
  EmptyState,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  PageHeader,
  RelativeTime,
  Spinner,
  Table,
  Td,
  Th,
  Tr,
} from '../../components';
import { FinishSetup } from './FinishSetup';
import { goTo, postManifest } from './githubNavigation';

/** The refetch key for the App status. */
export const GITHUB_APP_KEY = 'github-app';

/** The refetch key for the installations list. */
export const GITHUB_INSTALLATIONS_KEY = 'github-installations';

/** The page's crumbs. */
const CRUMBS = [{ label: 'Settings' }, { label: 'GitHub' }] as const;

/** The GitHub settings page. */
export function GitHubSettings(): React.ReactElement {
  const auth = useQueryAuth();
  const app = usePolledQuery<GitHubAppStatus>(
    ({ signal }) => api.getGitHubApp({ signal }),
    { intervalMs: 60_000, queryKey: GITHUB_APP_KEY, auth }
  );
  const status = app.data;

  return (
    <div className="space-y-6">
      <PageHeader
        title="GitHub"
        crumbs={CRUMBS}
        description="The GitHub App this environment uses to read repositories and report runs back to pull requests."
      />
      <ErrorNotice error={app.error} />
      {app.isLoading || status === null ? (
        app.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-text-faint">
            <Spinner label="Loading GitHub settings" className="size-4" />
            Loading GitHub settings
          </div>
        ) : null
      ) : status.configured ? (
        <>
          <AppSummary app={status} />
          <Installations app={status} />
        </>
      ) : (
        <CreateApp app={status} />
      )}
    </div>
  );
}

/** A bordered section with a heading. */
function Section({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <section
      aria-label={title}
      className="space-y-4 rounded-lg border border-line bg-panel p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-medium text-text-strong">{title}</h2>
          {description === undefined ? null : (
            <p className="mt-0.5 text-sm text-text-muted">{description}</p>
          )}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}

/** The create step, shown while no App is configured. */
function CreateApp({ app }: { app: GitHubAppStatus }): React.ReactElement {
  const [organization, setOrganization] = useState('');
  const { mutate, isMutating, error } = useMutationWithRefetch(
    (org: string) =>
      api.startGitHubManifest(org === '' ? {} : { organization: org }),
    GITHUB_APP_KEY
  );

  const submit = async (): Promise<void> => {
    try {
      const start = await mutate(organization.trim());
      postManifest(start.action_url, start.manifest);
    } catch {
      return;
    }
  };

  return (
    <Section
      title="No GitHub App yet"
      description="Create one from a manifest. GitHub asks you to confirm the name, then returns here and the credentials are stored for you."
    >
      {app.can_create ? (
        <form
          aria-label="Create a GitHub App"
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <Field
            label="Organization"
            hint="Leave empty to create the App on your personal account."
          >
            {(control) => (
              <input
                {...control}
                value={organization}
                placeholder="WebbPulse"
                pattern="[A-Za-z0-9][A-Za-z0-9\-]*"
                title="An organization login: letters, digits and hyphens."
                onChange={(event) => {
                  setOrganization(event.target.value);
                }}
                className={`${INPUT_CLASS} max-w-xs`}
              />
            )}
          </Field>
          <ErrorNotice error={error} />
          <Button
            type="submit"
            variant="primary"
            busy={isMutating}
            busyLabel="Preparing the App manifest"
          >
            Create GitHub App
          </Button>
        </form>
      ) : (
        <p className="text-sm text-text-muted">
          This environment cannot create an App from here.
        </p>
      )}
    </Section>
  );
}

/** The configured App, with its links and the logo step. */
function AppSummary({ app }: { app: GitHubAppStatus }): React.ReactElement {
  return (
    <Section
      title={app.name ?? app.slug ?? 'GitHub App'}
      description={
        app.owner_login === null || app.owner_login === undefined
          ? 'Configured for this environment.'
          : `Owned by ${app.owner_login}.`
      }
      actions={
        <div className="flex flex-wrap gap-2">
          {(app.html_url ?? '') === '' ? null : (
            <a
              href={app.html_url ?? undefined}
              target="_blank"
              rel="noreferrer"
              className="inline-flex h-8 items-center rounded-md border border-line px-3 text-sm text-text hover:bg-raised"
            >
              View on GitHub
            </a>
          )}
          {(app.settings_url ?? '') === '' ? null : (
            <a
              href={app.settings_url ?? undefined}
              target="_blank"
              rel="noreferrer"
              className="inline-flex h-8 items-center rounded-md border border-line px-3 text-sm text-text hover:bg-raised"
            >
              App settings
            </a>
          )}
        </div>
      }
    >
      <dl className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-xs text-text-faint">Slug</dt>
          <dd className="font-mono text-text">{app.slug ?? 'Not set'}</dd>
        </div>
        <div>
          <dt className="text-xs text-text-faint">App ID</dt>
          <dd className="font-mono text-text">{app.app_id ?? 'Unknown'}</dd>
        </div>
        <div>
          <dt className="text-xs text-text-faint">Created</dt>
          <dd className="text-text">
            {(app.created_at ?? '') === '' ? (
              'Before this page'
            ) : (
              <RelativeTime iso={app.created_at ?? ''} />
            )}
          </dd>
        </div>
      </dl>
      <details className="group rounded-md border border-line px-3 py-2">
        <summary className="cursor-pointer text-sm text-text">
          Logo and badge colour
        </summary>
        <div className="pt-3">
          <FinishSetup app={app} />
        </div>
      </details>
    </Section>
  );
}

/** The install button and the installations table. */
function Installations({ app }: { app: GitHubAppStatus }): React.ReactElement {
  const auth = useQueryAuth();
  const [removing, setRemoving] = useState<Installation | null>(null);
  const query = usePolledQuery<InstallationList>(
    ({ signal }) => api.listGitHubInstallations({ signal }),
    { intervalMs: 60_000, queryKey: GITHUB_INSTALLATIONS_KEY, auth }
  );
  const install = useMutationWithRefetch(
    () => api.startGitHubInstall(),
    GITHUB_INSTALLATIONS_KEY
  );
  const refresh = useMutationWithRefetch(
    (id: string) => api.refreshGitHubInstallation(id),
    GITHUB_INSTALLATIONS_KEY
  );
  const remove = useMutationWithRefetch(
    (id: string) => api.removeGitHubInstallation(id),
    GITHUB_INSTALLATIONS_KEY
  );

  const startInstall = async (): Promise<void> => {
    try {
      goTo((await install.mutate()).install_url);
    } catch {
      return;
    }
  };

  const items = query.data?.items ?? [];

  return (
    <Section
      title="Installations"
      description="The accounts the App is installed on. Repository access is chosen on GitHub."
      actions={
        <Button
          variant="primary"
          disabled={!app.can_install}
          busy={install.isMutating}
          busyLabel="Opening GitHub"
          onClick={() => {
            void startInstall();
          }}
        >
          Install on repositories
        </Button>
      }
    >
      {app.can_install ? null : (
        <p className="text-sm text-text-muted">
          Installing needs the App's slug. Set it and reload this page.
        </p>
      )}
      <ErrorNotice error={install.error ?? refresh.error ?? query.error} />
      {query.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Loading installations" className="size-4" />
          Loading installations
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          title="Not installed anywhere yet."
          hint="Install the App on an organization or account to choose its repositories."
        />
      ) : (
        <div className="space-y-3">
          <Table label="Installations">
            <thead>
              <tr>
                <Th>Account</Th>
                <Th>Repositories</Th>
                <Th>Status</Th>
                <Th>Checked</Th>
                <Th>
                  <span className="sr-only">Actions</span>
                </Th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <Tr key={item.installation_id}>
                  <Td>
                    <span className="font-medium text-text-strong">
                      {item.account_login}
                    </span>
                    <p className="text-xs text-text-faint">
                      {item.account_type}
                    </p>
                  </Td>
                  <Td className="text-text-muted">
                    {item.repository_selection === 'all'
                      ? 'All repositories'
                      : 'Selected repositories'}
                  </Td>
                  <Td>
                    {item.suspended ? (
                      <span className="text-xs text-danger">Suspended</span>
                    ) : (
                      <span className="text-xs text-success">Active</span>
                    )}
                  </Td>
                  <Td className="text-xs whitespace-nowrap text-text-faint">
                    <RelativeTime iso={item.updated_at} />
                  </Td>
                  <Td className="text-right whitespace-nowrap">
                    <div className="flex justify-end gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`Refresh ${item.account_login}`}
                        busy={refresh.isMutating}
                        busyLabel="Refreshing"
                        onClick={() => {
                          void refresh
                            .mutate(item.installation_id)
                            .catch(() => undefined);
                        }}
                      >
                        Refresh
                      </Button>
                      {(item.html_url ?? '') === '' ? null : (
                        <a
                          href={item.html_url ?? undefined}
                          target="_blank"
                          rel="noreferrer"
                          aria-label={`Configure ${item.account_login} on GitHub`}
                          className="inline-flex h-7 items-center rounded-md px-2.5 text-xs text-text hover:bg-raised"
                        >
                          Configure on GitHub
                        </a>
                      )}
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`Remove ${item.account_login}`}
                        onClick={() => {
                          setRemoving(item);
                        }}
                      >
                        Remove
                      </Button>
                    </div>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
          {items.map((item) => (
            <RepositoriesToggle
              key={item.installation_id}
              installation={item}
            />
          ))}
        </div>
      )}
      <Dialog
        open={removing !== null}
        onClose={() => {
          setRemoving(null);
        }}
        title="Remove this installation?"
        description="This only forgets it here. The App stays installed on GitHub until it is uninstalled there."
      >
        <ErrorNotice error={remove.error} />
        <div className="flex justify-end gap-2 pt-3">
          <Button
            variant="ghost"
            onClick={() => {
              setRemoving(null);
            }}
          >
            Cancel
          </Button>
          <Button
            variant="danger"
            busy={remove.isMutating}
            busyLabel="Removing"
            onClick={() => {
              if (removing === null) {
                return;
              }
              void remove
                .mutate(removing.installation_id)
                .then(() => {
                  setRemoving(null);
                })
                .catch(() => undefined);
            }}
          >
            Remove installation
          </Button>
        </div>
      </Dialog>
    </Section>
  );
}

/** A button that shows one installation's repositories, loading them only once opened. */
function RepositoriesToggle({
  installation,
}: {
  installation: Installation;
}): React.ReactElement {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-line px-3 py-2">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen(!open);
        }}
        className="w-full text-left text-sm text-text hover:text-text-strong"
      >
        Repositories in {installation.account_login}
      </button>
      {open ? (
        <RepositoryListing installationId={installation.installation_id} />
      ) : null}
    </div>
  );
}

/** The repositories one installation reaches, read only. */
function RepositoryListing({
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
    return (
      <div className="flex items-center gap-2 py-2 text-sm text-text-faint">
        <Spinner label="Loading repositories" className="size-4" />
        Loading repositories
      </div>
    );
  }
  if (query.error !== null && query.error !== undefined) {
    return <ErrorNotice error={query.error} className="mt-2" />;
  }
  const items = query.data?.items ?? [];
  if (items.length === 0) {
    return (
      <p className="py-2 text-sm text-text-faint">
        This installation reaches no repositories.
      </p>
    );
  }
  return (
    <ul
      aria-label={`Repositories for installation ${installationId}`}
      className="mt-2 divide-y divide-line rounded-md border border-line"
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
