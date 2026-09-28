/** Account security: turn an authenticator app on or off and replace its recovery codes. */

import { useMemo, useState } from 'react';
import type { AuthClient } from '@webbpulse/auth';
import { useTotpPanel } from '@webbpulse/auth/panels';
import { useAuthClient } from '@webbpulse/auth/react';
import { qrCodeSvgPath } from '@webbpulse/qrcode';

import {
  Button,
  CopyButton,
  Field,
  INPUT_CLASS,
  PageHeader,
} from '../../components';

/** The page's crumbs, the page itself left to the title. */
const CRUMBS = [{ label: 'Account' }] as const;

/** The sentence for each refusal the identity service may leave without one. */
export const REASON_FALLBACKS: Readonly<Record<string, string>> = {
  'invalid-code': 'That code is not valid. Check the app and try again.',
  'already-enabled':
    'This account already has an authenticator app. Turn it off before adding another.',
  'no-pending-enrolment':
    'That setup is no longer pending. Start again for a new key.',
  'rate-limited': 'Too many attempts. Wait a few minutes and try again.',
  unavailable: 'Authenticator apps are not available on this deployment.',
};

/** The refusal shape every MFA call can resolve to. */
interface Refusal {
  ok: false;
  reason: string;
  message: string;
  retryAfter?: number | undefined;
}

/** The sentence for a refusal, preferring the server's own. */
export function refusalMessage(refusal: Omit<Refusal, 'ok'>): string {
  const base =
    refusal.message.trim() !== ''
      ? refusal.message
      : (REASON_FALLBACKS[refusal.reason] ?? 'That request was refused.');
  return refusal.reason === 'rate-limited' && refusal.retryAfter !== undefined
    ? `${base} Try again in ${refusal.retryAfter} seconds.`
    : base;
}

/** Runs one MFA call, giving a refusal or a thrown error a sentence to show. */
async function described<T extends { ok: boolean }>(
  call: () => Promise<T>
): Promise<T> {
  try {
    const outcome = await call();
    if (outcome.ok) {
      return outcome;
    }
    const refusal = outcome as unknown as Refusal;
    return { ...outcome, message: refusalMessage(refusal) };
  } catch {
    return {
      ok: false,
      reason: 'failed',
      message: 'That request could not be completed. Try again.',
    } as unknown as T;
  }
}

/** The client with every MFA refusal carrying a sentence. */
function useDescribedClient(client: AuthClient<unknown>): AuthClient<unknown> {
  return useMemo(() => {
    const overrides: Partial<AuthClient<unknown>> = {
      enrolTotp: () => described(() => client.enrolTotp()),
      activateTotp: (input) => described(() => client.activateTotp(input)),
      disableTotp: (input) => described(() => client.disableTotp(input)),
      regenerateRecoveryCodes: (input) =>
        described(() => client.regenerateRecoveryCodes(input)),
    };
    return Object.assign(
      Object.create(client) as AuthClient<unknown>,
      overrides
    );
  }, [client]);
}

/** The provisioning URI as a QR code, with the setup key under it. */
function ProvisioningQr({
  uri,
  secret,
}: {
  uri: string;
  secret: string;
}): React.ReactElement {
  let drawing: { path: string; viewBox: string } | null;
  try {
    drawing = qrCodeSvgPath(uri);
  } catch {
    drawing = null;
  }
  return (
    <div className="space-y-3">
      {drawing === null ? (
        <p className="text-sm text-text-muted">
          This setup is too long to draw as a QR code. Enter the key below by
          hand.
        </p>
      ) : (
        <svg
          role="img"
          aria-label="QR code for the authenticator app"
          viewBox={drawing.viewBox}
          className="size-48 rounded-md bg-white p-2"
          shapeRendering="crispEdges"
        >
          <path d={drawing.path} fill="#000000" />
        </svg>
      )}
      <div className="text-sm">
        <p className="text-text-muted">Setup key</p>
        <div className="mt-1 flex items-center gap-2">
          <code
            data-testid="totp-secret"
            className="flex-1 rounded-md border border-line bg-raised px-2.5 py-1.5 font-mono text-xs break-all text-text-strong"
          >
            {secret}
          </code>
          <CopyButton value={secret} subject="setup key" />
        </div>
        <p className="mt-2 text-xs text-text-faint">
          Scan the code or enter the key by hand. It is shown once and cannot be
          read back.
        </p>
      </div>
    </div>
  );
}

/** The one time recovery codes, behind an explicit confirmation. */
function RecoveryCodes({
  codes,
  onConfirm,
}: {
  codes: string[];
  onConfirm: () => void;
}): React.ReactElement {
  const [saved, setSaved] = useState(false);
  return (
    <section className="space-y-4 rounded-lg border border-line bg-panel p-4">
      <div>
        <h2 className="text-sm font-medium text-text-strong">Recovery codes</h2>
        <p className="mt-1 text-sm text-text-muted">
          Keep these somewhere you can reach without your phone. Each works
          once, in the same field as an app code. They are shown only now.
        </p>
      </div>
      <ul
        data-testid="recovery-codes"
        className="grid grid-cols-1 gap-2 rounded-md bg-raised p-3 sm:grid-cols-2"
      >
        {codes.map((code) => (
          <li key={code} className="font-mono text-sm text-text-strong">
            {code}
          </li>
        ))}
      </ul>
      <CopyButton
        value={codes.join('\n')}
        label="Copy codes"
        variant="secondary"
      />
      <label className="flex items-center gap-2 text-sm text-text-muted">
        <input
          type="checkbox"
          checked={saved}
          onChange={(event) => {
            setSaved(event.target.checked);
          }}
        />
        I have saved these codes
      </label>
      <Button variant="primary" onClick={onConfirm} disabled={!saved}>
        Done
      </Button>
    </section>
  );
}

/** Props for {@link CodePrompt}. */
interface CodePromptProps {
  title: string;
  hint: string;
  submitLabel: string;
  busy: boolean;
  code: string;
  onCodeChange: (code: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
}

/** A small form that takes one code and runs an action with it. */
function CodePrompt({
  title,
  hint,
  submitLabel,
  busy,
  code,
  onCodeChange,
  onSubmit,
  onCancel,
}: CodePromptProps): React.ReactElement {
  return (
    <form
      className="space-y-3 rounded-lg border border-line bg-panel p-4"
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <h2 className="text-sm font-medium text-text-strong">{title}</h2>
      <Field label="Authenticator or recovery code" hint={hint}>
        {(control) => (
          <input
            {...control}
            type="text"
            inputMode="text"
            autoComplete="one-time-code"
            className={INPUT_CLASS}
            value={code}
            required
            disabled={busy}
            onChange={(event) => {
              onCodeChange(event.target.value);
            }}
          />
        )}
      </Field>
      <div className="flex gap-2">
        <Button type="submit" variant="primary" busy={busy}>
          {submitLabel}
        </Button>
        <Button variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

/** The account security page. */
export function Security(): React.ReactElement {
  const client = useDescribedClient(useAuthClient());
  const totp = useTotpPanel({
    client,
    messages: {
      disabled: 'The authenticator app is off and its recovery codes are void.',
      saved: 'The authenticator app is on. Sign-in now asks for a code.',
    },
  });
  const { factor, step, prompt, code, busy, error, notice, setCode } = totp;

  const status =
    factor === 'enabled'
      ? 'An authenticator app is on for this account.'
      : factor === 'disabled'
        ? 'No authenticator app is on for this account.'
        : 'Sign-in asks for a code once an authenticator app is on. This page shows only what it has changed since it opened.';

  return (
    <div className="max-w-xl space-y-6">
      <PageHeader
        title="Security"
        crumbs={CRUMBS}
        description="A second factor for signing in: a six digit code from an authenticator app, with one time recovery codes as a fallback."
      />
      {error === null ? null : (
        <p
          role="alert"
          className="rounded-md border border-danger-line bg-danger-soft px-3 py-2 text-sm text-danger"
        >
          {error}
        </p>
      )}
      {notice === null ? null : (
        <p
          role="status"
          className="rounded-md border border-line bg-accent-soft px-3 py-2 text-sm text-text-strong"
        >
          {notice}
        </p>
      )}
      <p data-testid="factor-status" className="text-sm text-text-muted">
        {status}
      </p>
      {step.kind === 'codes' ? (
        <RecoveryCodes codes={step.codes} onConfirm={totp.acknowledgeCodes} />
      ) : step.kind === 'scanning' ? (
        <div className="space-y-4">
          <ProvisioningQr uri={step.provisioningUri} secret={step.secret} />
          <CodePrompt
            title="Confirm the app"
            hint="Enter the code the app shows now. This turns the factor on and issues recovery codes."
            submitLabel="Turn on"
            busy={busy}
            code={code}
            onCodeChange={setCode}
            onSubmit={() => void totp.activate()}
            onCancel={totp.reset}
          />
        </div>
      ) : prompt === 'disable' ? (
        <CodePrompt
          title="Turn off the authenticator app"
          hint="Enter a current app code or a recovery code. Every recovery code is voided too."
          submitLabel="Turn off"
          busy={busy}
          code={code}
          onCodeChange={setCode}
          onSubmit={() => void totp.disable()}
          onCancel={totp.reset}
        />
      ) : prompt === 'regenerate' ? (
        <CodePrompt
          title="Generate new recovery codes"
          hint="Enter a current app code or a remaining recovery code. The new set replaces the old one."
          submitLabel="Generate"
          busy={busy}
          code={code}
          onCodeChange={setCode}
          onSubmit={() => void totp.regenerate()}
          onCancel={totp.reset}
        />
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button
            variant="primary"
            busy={busy}
            onClick={() => void totp.enrol()}
          >
            Set up an authenticator app
          </Button>
          <Button
            onClick={() => {
              totp.ask('regenerate');
            }}
            disabled={busy}
          >
            New recovery codes
          </Button>
          <Button
            variant="danger"
            onClick={() => {
              totp.ask('disable');
            }}
            disabled={busy}
          >
            Turn off
          </Button>
        </div>
      )}
    </div>
  );
}
