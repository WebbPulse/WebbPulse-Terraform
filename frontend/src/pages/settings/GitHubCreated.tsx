/** Where GitHub returns after creating the App: store its credentials, then finish setup. */

import { useCallback } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useMutationWithRefetch } from '@webbpulse/api-client/react';

import { api, type GitHubAppStatus } from '../../api';
import { Button, ErrorNotice, PageHeader, Spinner } from '../../components';
import { FinishSetup } from './FinishSetup';
import { GITHUB_APP_KEY } from './GitHubSettings';
import { goTo } from './githubNavigation';
import { useCallbackOnce } from './useCallbackOnce';

/** The page's crumbs. */
const CRUMBS = [
  { label: 'Settings' },
  { label: 'GitHub', to: '/settings/github' },
] as const;

/** The create callback page. */
export function GitHubCreated(): React.ReactElement {
  const [params] = useSearchParams();
  const code = params.get('code') ?? '';
  const state = params.get('state') ?? '';
  const run = useCallback(
    () =>
      code === '' || state === ''
        ? null
        : api.convertGitHubManifest({ code, state }),
    [code, state]
  );
  const result = useCallbackOnce<GitHubAppStatus>(run);

  return (
    <div className="space-y-6">
      <PageHeader title="Finish setup" crumbs={CRUMBS} />
      {result.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-text-faint">
          <Spinner label="Storing the App credentials" className="size-4" />
          Storing the App credentials
        </div>
      ) : result.data === null ? (
        <div className="space-y-3">
          {result.error === null ? (
            <p role="alert" className="text-sm text-text-muted">
              This page is where GitHub returns after creating the App, and the
              link is missing its code. Start again from GitHub settings.
            </p>
          ) : (
            <ErrorNotice error={result.error} />
          )}
          <Link
            to="/settings/github"
            className="text-sm text-accent hover:underline"
          >
            Back to GitHub settings
          </Link>
        </div>
      ) : (
        <Created app={result.data} />
      )}
    </div>
  );
}

/** The App was created: the manual logo steps, then install. */
function Created({ app }: { app: GitHubAppStatus }): React.ReactElement {
  const install = useMutationWithRefetch(
    () => api.startGitHubInstall(),
    GITHUB_APP_KEY
  );
  const startInstall = async (): Promise<void> => {
    try {
      goTo((await install.mutate()).install_url);
    } catch {
      return;
    }
  };
  return (
    <div className="space-y-4">
      <section
        aria-label="App created"
        className="space-y-1 rounded-lg border border-success-line bg-success-soft px-4 py-3"
      >
        <p className="text-sm font-medium text-text-strong">
          {app.name ?? app.slug} is created and its credentials are stored.
        </p>
        <p className="text-sm text-text-muted">
          Two manual steps give it the WebbPulse Terraform logo, then install it
          on the repositories it should reach.
        </p>
      </section>
      <section
        aria-label="Logo and badge"
        className="rounded-lg border border-line bg-panel p-4"
      >
        <FinishSetup app={app} />
      </section>
      <ErrorNotice error={install.error} />
      <div className="flex flex-wrap items-center gap-3">
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
        <Link
          to="/settings/github"
          className="text-sm text-text-muted hover:text-text-strong"
        >
          Later
        </Link>
      </div>
    </div>
  );
}
