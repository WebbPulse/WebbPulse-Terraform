/** Where GitHub returns after an install: confirm it with GitHub, store it, and show what it reaches. */

import { useCallback } from 'react';
import { Link } from 'react-router-dom';

import { api, type Installation } from '../../api';
import { ErrorNotice, PageHeader, Spinner } from '../../components';
import { repositoryAccess } from './repositoryAccess';
import { RepositoryListing } from './RepositoryListing';
import { useCallbackOnce } from './useCallbackOnce';
import { useClearQueryWhen, useInitialParams } from './useCallbackParams';

/** The page's crumbs. */
const CRUMBS = [
  { label: 'Settings' },
  { label: 'GitHub', to: '/settings/github' },
] as const;

/** The classes of the link back to settings, styled as the primary button. */
const PRIMARY_LINK_CLASS =
  'inline-flex h-8 items-center rounded-md bg-accent px-3 text-sm font-medium text-accent-contrast hover:bg-accent-hover';

/** The setup callback page. */
export function GitHubSetup(): React.ReactElement {
  const params = useInitialParams();
  const installationId = Number(params.get('installation_id') ?? '');
  const setupAction = params.get('setup_action');
  const state = params.get('state');
  const requested = setupAction === 'request';
  const run = useCallback(
    () =>
      requested || !Number.isInteger(installationId) || installationId <= 0
        ? null
        : api.recordGitHubInstallation({
            installation_id: installationId,
            setup_action: setupAction,
            state,
          }),
    [installationId, requested, setupAction, state]
  );
  const result = useCallbackOnce<Installation>(run);
  useClearQueryWhen(!result.isLoading);

  return (
    <div className="space-y-6">
      <PageHeader title="Install the GitHub App" crumbs={CRUMBS} />
      {result.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Confirming the installation" className="size-4" />
          Confirming the installation with GitHub
        </div>
      ) : result.data !== null ? (
        <Installed
          installation={result.data}
          updated={setupAction === 'update'}
        />
      ) : (
        <div className="space-y-3">
          {requested ? (
            <p role="status" className="text-sm text-text-muted">
              The install was requested. An owner of the account has to approve
              it on GitHub, and it shows up here after that.
            </p>
          ) : result.error === null ? (
            <p role="alert" className="text-sm text-text-muted">
              This page is where GitHub returns after an install, and the link
              is missing its installation. Start again from GitHub settings.
            </p>
          ) : (
            <ErrorNotice error={result.error} />
          )}
          <Link
            to="/settings/github"
            className="inline-block text-sm text-accent hover:underline"
          >
            Back to GitHub settings
          </Link>
        </div>
      )}
    </div>
  );
}

/** The confirmed installation, its repositories and what to do next. */
function Installed({
  installation,
  updated,
}: {
  installation: Installation;
  updated: boolean;
}): React.ReactElement {
  return (
    <div className="space-y-4">
      <section
        aria-label="Installed"
        className="space-y-1 rounded-lg border border-success-line bg-success-soft px-4 py-3 text-sm"
      >
        <p className="font-medium text-text-strong">
          {updated
            ? `Updated the installation on ${installation.account_login}.`
            : `Installed on ${installation.account_login}.`}
        </p>
        <p className="text-text-muted">
          {installation.repository_selection === 'all'
            ? 'It can reach every repository in the account.'
            : 'It can reach the repositories chosen on GitHub.'}
        </p>
      </section>
      <section
        aria-label="Repositories"
        className="space-y-3 rounded-lg border border-line bg-panel p-4"
      >
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-medium text-text-strong">Repositories</h2>
          <span className="text-xs text-text-faint">
            {repositoryAccess(installation)}
          </span>
        </div>
        <RepositoryListing installationId={installation.installation_id} />
      </section>
      <section
        aria-label="Next step"
        className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-panel p-4"
      >
        <div>
          <h2 className="text-sm font-medium text-text-strong">Next step</h2>
          <p className="mt-0.5 text-sm text-text-muted">
            Workspaces can now be connected to these repositories.
          </p>
        </div>
        <Link to="/settings/github" className={PRIMARY_LINK_CLASS}>
          Back to GitHub settings
        </Link>
      </section>
    </div>
  );
}
