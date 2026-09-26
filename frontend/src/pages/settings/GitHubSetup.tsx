/** Where GitHub returns after an install: confirm it with GitHub and store it. */

import { useCallback } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { api, type Installation } from '../../api';
import { ErrorNotice, PageHeader, Spinner } from '../../components';
import { useCallbackOnce } from './useCallbackOnce';

/** The page's crumbs. */
const CRUMBS = [
  { label: 'Settings' },
  { label: 'GitHub', to: '/settings/github' },
] as const;

/** The setup callback page. */
export function GitHubSetup(): React.ReactElement {
  const [params] = useSearchParams();
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

  return (
    <div className="space-y-6">
      <PageHeader title="Install the GitHub App" crumbs={CRUMBS} />
      {result.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Confirming the installation" className="size-4" />
          Confirming the installation with GitHub
        </div>
      ) : result.data !== null ? (
        <section
          aria-label="Installed"
          className="rounded-lg border border-success-line bg-success-soft px-4 py-3 text-sm"
        >
          <p className="font-medium text-text-strong">
            Installed on {result.data.account_login}.
          </p>
          <p className="text-text-muted">
            {result.data.repository_selection === 'all'
              ? 'It can reach every repository in the account.'
              : 'It can reach the repositories chosen on GitHub.'}
          </p>
        </section>
      ) : requested ? (
        <p role="status" className="text-sm text-text-muted">
          The install was requested. An owner of the account has to approve it
          on GitHub, and it shows up here after that.
        </p>
      ) : result.error === null ? (
        <p role="alert" className="text-sm text-text-muted">
          This page is where GitHub returns after an install, and the link is
          missing its installation. Start again from GitHub settings.
        </p>
      ) : (
        <ErrorNotice error={result.error} />
      )}
      {result.isLoading ? null : (
        <Link
          to="/settings/github"
          className="inline-block text-sm text-accent hover:underline"
        >
          Back to GitHub settings
        </Link>
      )}
    </div>
  );
}
