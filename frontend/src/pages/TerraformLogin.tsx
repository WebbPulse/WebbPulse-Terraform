/**
 * The approve page `terraform login` opens, like the token page
 * `terraform login app.terraform.io` opens.
 *
 * Terraform CLI sends the browser here with its PKCE challenge and a loopback
 * redirect. Approving asks the API for a code, behind the step-up prompt since it
 * mints a key, and hands the browser back to Terraform's listener, which exchanges
 * the code for the key and stores it in `credentials.tfrc.json`.
 */

import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { api } from '../api';
import { Button, ErrorNotice, Mark, buttonClass } from '../components';
import { isLoopback, loginRequest } from './terraformLoginRequest';

/** The listener's `host:port`, shown so the person can match it to their terminal. */
function listener(redirectUri: string): string {
  try {
    return new URL(redirectUri).host;
  } catch {
    return redirectUri;
  }
}

/** The approve page. */
export function TerraformLogin(): React.ReactElement {
  const [search] = useSearchParams();
  const request = loginRequest(search);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  const host = window.location.host;

  const approve = async (): Promise<void> => {
    if (request === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const approved = await api.createTerraformLoginAuthorization(request);
      if (!isLoopback(approved.redirect_url)) {
        throw new Error('The approval pointed somewhere other than Terraform.');
      }
      setDone(true);
      window.location.assign(approved.redirect_url);
    } catch (thrown) {
      setError(thrown);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center gap-2.5">
          <Mark className="size-7 text-xs" />
          <h1 className="text-base font-semibold text-text-strong">
            WebbPulse Terraform
          </h1>
        </div>
        <div className="space-y-4 rounded-lg border border-line bg-panel p-6 shadow-sm">
          <div>
            <h2 className="text-sm font-medium text-text-strong">
              Terraform login
            </h2>
            <p className="mt-0.5 text-xs text-text-faint">
              {'Terraform CLI is asking for an API token for '}
              <span className="font-mono">{host}</span>.
            </p>
          </div>
          {request === null ? (
            <p className="text-sm text-text-muted">
              {'This page is opened by '}
              <span className="font-mono">terraform login {host}</span>
              {'. Run that in a terminal to sign Terraform in.'}
            </p>
          ) : done ? (
            <p className="text-sm text-text-muted" data-testid="login-done">
              Approved. Terraform has the token; you can close this tab and
              return to your terminal.
            </p>
          ) : (
            <>
              <ul className="list-disc space-y-1 pl-5 text-sm text-text">
                <li>
                  {'Creates an API key named '}
                  <span className="font-mono">terraform login</span>
                  {' that expires in 90 days.'}
                </li>
                <li>
                  It can read workspaces, variables, runs and the registry, and
                  start plans, within what your account holds.
                </li>
                <li>It cannot confirm an apply or download raw state.</li>
                <li>
                  {'The token goes to Terraform listening on '}
                  <span className="font-mono">
                    {listener(request.redirect_uri)}
                  </span>
                  .
                </li>
              </ul>
              <ErrorNotice error={error} />
              <div className="flex justify-end gap-2">
                <Link to="/workspaces" className={buttonClass('ghost')}>
                  Cancel
                </Link>
                <Button
                  variant="primary"
                  busy={busy}
                  busyLabel="Approving"
                  onClick={() => {
                    void approve();
                  }}
                >
                  Approve
                </Button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
