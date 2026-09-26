/** The manual step after creating the App: its logo and badge colour, which GitHub has no API for. */

import type { GitHubAppStatus } from '../../api';
import { CopyButton } from '../../components';

/** The filename the logo downloads as. */
export const LOGO_FILENAME = 'webbpulse-terraform-github-app.png';

/** Props for {@link FinishSetup}. */
export interface FinishSetupProps {
  app: GitHubAppStatus;
}

/**
 * Where to upload the logo and which badge colour to set.
 *
 * GitHub only takes an App logo through its settings page, and shows the badge
 * background field once a logo is uploaded, so this hands over the file and the
 * hex and links to the page rather than doing either.
 */
export function FinishSetup({ app }: FinishSetupProps): React.ReactElement {
  return (
    <ol className="space-y-4 text-sm">
      <li className="flex gap-3">
        <StepNumber n={1} />
        <div className="min-w-0 flex-1 space-y-2">
          <p className="font-medium text-text-strong">Upload the logo</p>
          <p className="text-text-muted">
            Open the App's settings, find Display information and choose Upload
            a logo. GitHub has no API for this, so it is done by hand once.
          </p>
          <div className="flex flex-wrap items-center gap-3">
            <img
              src={app.logo_path}
              alt="WebbPulse Terraform App logo"
              className="size-12 rounded-md border border-line"
            />
            <a
              href={app.logo_path}
              download={LOGO_FILENAME}
              className="inline-flex h-8 items-center rounded-md border border-line px-3 text-sm text-text hover:bg-raised"
            >
              Download logo
            </a>
            {(app.settings_url ?? '') === '' ? null : (
              <a
                href={app.settings_url ?? undefined}
                target="_blank"
                rel="noreferrer"
                className="inline-flex h-8 items-center rounded-md bg-accent px-3 text-sm font-medium text-accent-contrast hover:bg-accent-hover"
              >
                Open App settings
              </a>
            )}
          </div>
        </div>
      </li>
      <li className="flex gap-3">
        <StepNumber n={2} />
        <div className="min-w-0 flex-1 space-y-2">
          <p className="font-medium text-text-strong">
            Set the badge background
          </p>
          <p className="text-text-muted">
            After the upload GitHub shows Badge background color. Set it to this
            value and save.
          </p>
          <div className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className="inline-block size-5 rounded border border-line"
              style={{ backgroundColor: app.badge_background }}
            />
            <code className="font-mono text-sm text-text-strong">
              {app.badge_background}
            </code>
            <CopyButton
              value={app.badge_background}
              subject="badge background"
            />
          </div>
        </div>
      </li>
    </ol>
  );
}

/** A step's number in a small circle. */
function StepNumber({ n }: { n: number }): React.ReactElement {
  return (
    <span
      aria-hidden="true"
      className="flex size-6 shrink-0 items-center justify-center rounded-full border border-line text-xs text-text-muted"
    >
      {n}
    </span>
  );
}
