/** Global settings for GitHub: create the App, point its webhook here, install it and see what it reaches. Admin only. */

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
  type WebhookConfig,
} from '../../api';
import {
  Button,
  CopyButton,
  Dialog,
  EmptyState,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  PageHeader,
  RelativeTime,
  Table,
  Td,
  Th,
  Tr,
} from '../../components';
import { Collapsible } from './Collapsible';
import { FinishSetup } from './FinishSetup';
import { goTo, postManifest } from './githubNavigation';
import { repositoryAccess } from './repositoryAccess';
import { RepositoryListing } from './RepositoryListing';
import { RowsSkeleton, SectionSkeleton } from './Skeleton';

/** The refetch key for the App status. */
export const GITHUB_APP_KEY = 'github-app';

/** The refetch key for the installations list. */
export const GITHUB_INSTALLATIONS_KEY = 'github-installations';

/** The page's crumbs, the page itself left to the title. */
const CRUMBS = [{ label: 'Settings' }] as const;

/** The organization the create form starts with. */
const DEFAULT_ORGANIZATION = 'WebbPulse';

/** How GitHub's settings page names each webhook event the bridge needs. */
const EVENT_LABELS: Readonly<Record<string, string>> = {
  push: 'Pushes',
  pull_request: 'Pull requests',
};

/** Who owns a new App: an organization, or the signed-in GitHub account. */
type Owner = 'organization' | 'personal';

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
          <div className="space-y-6">
            <SectionSkeleton label="Loading GitHub settings" lines={2} />
            <SectionSkeleton label="Loading installations" lines={3} />
          </div>
        ) : null
      ) : status.configured ? (
        <>
          <AppSummary app={status} />
          <Webhook app={status} />
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
  const [owner, setOwner] = useState<Owner>('organization');
  const [organization, setOrganization] = useState(DEFAULT_ORGANIZATION);
  const { mutate, isMutating, error } = useMutationWithRefetch(
    (org: string | null) =>
      api.startGitHubManifest(org === null ? {} : { organization: org }),
    GITHUB_APP_KEY
  );

  const submit = async (): Promise<void> => {
    try {
      const start = await mutate(
        owner === 'organization' ? organization.trim() : null
      );
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
          <fieldset className="space-y-2 text-sm">
            <legend className="text-text-muted">Owner</legend>
            <OwnerOption
              value="organization"
              current={owner}
              onChange={setOwner}
              label="Organization"
              hint="Recommended. The organization owns the App, so it outlives any one person's account."
            />
            <OwnerOption
              value="personal"
              current={owner}
              onChange={setOwner}
              label="Personal account"
              hint="The App belongs to the GitHub account you are signed in to."
            />
          </fieldset>
          {owner === 'organization' ? (
            <Field
              label="Organization name"
              hint="You need to be an owner of this organization on GitHub."
            >
              {(control) => (
                <input
                  {...control}
                  value={organization}
                  required
                  pattern="[A-Za-z0-9][A-Za-z0-9\-]*"
                  title="An organization login: letters, digits and hyphens."
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(event) => {
                    setOrganization(event.target.value);
                  }}
                  className={`${INPUT_CLASS} max-w-xs font-mono`}
                />
              )}
            </Field>
          ) : null}
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

/** One owner choice: a radio with its label and a line under it. */
function OwnerOption({
  value,
  current,
  onChange,
  label,
  hint,
}: {
  value: Owner;
  current: Owner;
  onChange: (owner: Owner) => void;
  label: string;
  hint: string;
}): React.ReactElement {
  const checked = value === current;
  return (
    <label
      className={`flex max-w-md cursor-pointer gap-3 rounded-md border px-3 py-2 transition-colors ${
        checked
          ? 'border-accent bg-raised/40'
          : 'border-line hover:bg-raised/40'
      }`}
    >
      <input
        type="radio"
        name="owner"
        value={value}
        checked={checked}
        onChange={() => {
          onChange(value);
        }}
        className="mt-0.5 accent-accent"
      />
      <span>
        <span className="block text-text-strong">{label}</span>
        <span className="block text-xs text-text-faint">{hint}</span>
      </span>
    </label>
  );
}

/** The configured App, with its links and the logo step. */
function AppSummary({ app }: { app: GitHubAppStatus }): React.ReactElement {
  const [logoOpen, setLogoOpen] = useState(false);
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
      <Collapsible
        title="Logo and badge colour"
        open={logoOpen}
        onToggle={() => {
          setLogoOpen(!logoOpen);
        }}
      >
        <FinishSetup app={app} />
      </Collapsible>
    </Section>
  );
}

/** The App's webhook: one click points it at this API and sets its signing secret. */
function Webhook({ app }: { app: GitHubAppStatus }): React.ReactElement {
  const [synced, setSynced] = useState<WebhookConfig | null>(null);
  const sync = useMutationWithRefetch(
    () => api.syncGitHubWebhook(),
    GITHUB_APP_KEY
  );

  const run = async (): Promise<void> => {
    try {
      setSynced(await sync.mutate());
    } catch {
      return;
    }
  };

  const settingsUrl = app.settings_url ?? '';

  return (
    <Section
      title="Webhook"
      description="Where GitHub sends push and pull request events. Syncing points the App's hook at this API and sets its signing secret."
      actions={
        <div className="flex items-center gap-3">
          {synced !== null && sync.error == null && !sync.isMutating ? (
            <span
              role="status"
              className="inline-flex items-center gap-1.5 text-sm text-success"
            >
              <span
                aria-hidden="true"
                className="size-1.5 rounded-full bg-success"
              />
              Webhook synced.
            </span>
          ) : null}
          <Button
            variant="primary"
            busy={sync.isMutating}
            busyLabel="Syncing the webhook"
            onClick={() => {
              void run();
            }}
          >
            Sync webhook
          </Button>
        </div>
      }
    >
      <ErrorNotice error={sync.error} />
      {synced === null ? (
        <p className="text-sm text-text-muted">
          GitHub does not report the current hook here. Sync to set it and see
          where deliveries go.
        </p>
      ) : (
        <dl
          aria-label="Webhook configuration"
          className="grid grid-cols-1 gap-3 text-sm sm:grid-cols-3"
        >
          <div className="sm:col-span-3">
            <dt className="text-xs text-text-faint">Payload URL</dt>
            <dd className="flex items-center gap-2">
              <span className="font-mono break-all text-text">
                {synced.url}
              </span>
              <CopyButton value={synced.url} subject="payload URL" />
            </dd>
          </div>
          <div>
            <dt className="text-xs text-text-faint">Content type</dt>
            <dd className="font-mono text-text">
              {synced.content_type === 'json'
                ? 'application/json'
                : synced.content_type}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-text-faint">SSL verification</dt>
            <dd
              className={
                synced.insecure_ssl === '0' ? 'text-text' : 'text-warning'
              }
            >
              {synced.insecure_ssl === '0' ? 'Enabled' : 'Disabled'}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-text-faint">Events</dt>
            <dd className="text-text">
              {synced.events
                .map((event) => EVENT_LABELS[event] ?? event)
                .join(', ')}
            </dd>
          </div>
        </dl>
      )}
      <p className="text-xs text-text-faint">
        GitHub has no API for the Active setting or the event subscriptions.
        Check both on the{' '}
        {settingsUrl === '' ? (
          "App's settings page"
        ) : (
          <a
            href={settingsUrl}
            target="_blank"
            rel="noreferrer"
            className="text-accent hover:underline"
          >
            App's settings page
          </a>
        )}
        .
      </p>
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
        <RowsSkeleton label="Loading installations" rows={2} />
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
                  <Td className="whitespace-nowrap text-text-muted">
                    {repositoryAccess(item)}
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

/** One installation's repositories behind a disclosure, loaded only once opened. */
function RepositoriesToggle({
  installation,
}: {
  installation: Installation;
}): React.ReactElement {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible
      title={`Repositories in ${installation.account_login}`}
      summary={repositoryAccess(installation)}
      open={open}
      onToggle={() => {
        setOpen(!open);
      }}
    >
      <RepositoryListing installationId={installation.installation_id} />
    </Collapsible>
  );
}
