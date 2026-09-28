/** The sign-in page: a password or passkey leg, then a TOTP leg when one is asked for. */

import { useState } from 'react';
import type { PasskeySignInOutcome } from '@webbpulse/auth';
import {
  useAuth,
  useAuthClient,
  usePasskeySignInButton,
} from '@webbpulse/auth/react';
import {
  PASSKEY_AVAILABILITY_PATH,
  identityUrl,
  passkeyLoginAvailability,
} from '@webbpulse/discovery';

import { API_BASE_URL, identityOriginFrom } from '../api';
import { Button, ErrorNotice, Field, INPUT_CLASS, Mark } from '../components';

/** Asks the identity service whether a passkey is a way in on this deployment. */
function probePasskeyLogin(): Promise<'available' | 'unavailable' | 'unknown'> {
  return passkeyLoginAvailability(
    identityUrl(identityOriginFrom(API_BASE_URL), PASSKEY_AVAILABILITY_PATH)
  );
}

/** Props for {@link SignIn}. */
export interface SignInProps {
  /** Whether to arm passkey autofill on mount. Off in tests. */
  conditionalPasskey?: boolean;
}

/** The sign-in form, with the MFA step the first leg can ask for. */
export function SignIn({
  conditionalPasskey = true,
}: SignInProps = {}): React.ReactElement {
  const { login, completeTotp, isBusy } = useAuth();
  const client = useAuthClient();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [ticket, setTicket] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const submitPassword = async (): Promise<void> => {
    setError(null);
    try {
      const outcome = await login({ email, password });
      if (outcome.mfaRequired) {
        setTicket(outcome.ticket ?? '');
      }
    } catch (thrown) {
      setError(thrown);
    }
  };

  const handlePasskey = (result: PasskeySignInOutcome): void => {
    if (!result.ok) {
      setError(new Error(result.message));
      return;
    }
    setError(null);
    if (result.kind === 'mfa-required') {
      setTicket(result.ticket);
    }
  };

  const passkey = usePasskeySignInButton({
    client,
    probe: probePasskeyLogin,
    email,
    conditional: conditionalPasskey,
    onResult: handlePasskey,
    onError: setError,
  });

  const submitCode = async (): Promise<void> => {
    if (ticket === null) {
      return;
    }
    setError(null);
    try {
      await completeTotp({ ticket, code });
    } catch (thrown) {
      setError(thrown);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-2.5">
          <Mark className="size-7 text-xs" />
          <h1 className="text-base font-semibold text-text-strong">
            WebbPulse Terraform
          </h1>
        </div>
        <div className="space-y-4 rounded-lg border border-line bg-panel p-6 shadow-sm">
          <div>
            <h2 className="text-sm font-medium text-text-strong">
              {ticket === null ? 'Sign in' : 'Second factor'}
            </h2>
            <p className="mt-0.5 text-xs text-text-faint">
              {ticket === null
                ? 'Use the account your administrator gave you, or a passkey you added to it.'
                : 'Enter the code from your authenticator app, or a recovery code.'}
            </p>
          </div>
          <ErrorNotice error={error} />
          {ticket === null ? (
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                void submitPassword();
              }}
            >
              <Field label="Email">
                {(control) => (
                  <input
                    {...control}
                    type="email"
                    required
                    autoFocus
                    autoComplete={
                      passkey.conditional ? 'username webauthn' : 'username'
                    }
                    value={email}
                    onChange={(event) => {
                      setEmail(event.target.value);
                    }}
                    className={INPUT_CLASS}
                  />
                )}
              </Field>
              <Field label="Password">
                {(control) => (
                  <input
                    {...control}
                    type="password"
                    required
                    autoComplete="current-password"
                    value={password}
                    onChange={(event) => {
                      setPassword(event.target.value);
                    }}
                    className={INPUT_CLASS}
                  />
                )}
              </Field>
              <Button
                type="submit"
                variant="primary"
                busy={isBusy}
                busyLabel="Signing in"
                className="w-full"
              >
                Sign in
              </Button>
              {passkey.offered ? (
                <>
                  <div
                    aria-hidden="true"
                    className="flex items-center gap-3 text-xs text-text-faint"
                  >
                    <span className="h-px flex-1 bg-line" />
                    or
                    <span className="h-px flex-1 bg-line" />
                  </div>
                  <Button
                    className="w-full"
                    busy={passkey.busy}
                    busyLabel="Waiting for your passkey"
                    disabled={isBusy}
                    onClick={() => {
                      setError(null);
                      void passkey.signIn();
                    }}
                  >
                    Sign in with passkey
                  </Button>
                </>
              ) : null}
            </form>
          ) : (
            <form
              className="space-y-3"
              onSubmit={(event) => {
                event.preventDefault();
                void submitCode();
              }}
            >
              <Field label="Authentication or recovery code">
                {(control) => (
                  <input
                    {...control}
                    inputMode="numeric"
                    required
                    autoFocus
                    autoComplete="one-time-code"
                    value={code}
                    onChange={(event) => {
                      setCode(event.target.value);
                    }}
                    className={`${INPUT_CLASS} font-mono tracking-widest`}
                  />
                )}
              </Field>
              <Button
                type="submit"
                variant="primary"
                busy={isBusy}
                busyLabel="Verifying"
                className="w-full"
              >
                Verify
              </Button>
              <Button
                variant="ghost"
                className="w-full"
                onClick={() => {
                  setTicket(null);
                  setCode('');
                  setError(null);
                }}
              >
                Back
              </Button>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
