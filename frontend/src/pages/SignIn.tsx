/** The sign-in page: a password leg, then a TOTP leg when one is asked for. */

import { useState } from 'react';
import { useAuth } from '@webbpulse/auth/react';

import { Button, ErrorNotice, Field, INPUT_CLASS, Mark } from '../components';

/** The sign-in form, with the MFA step the first leg can ask for. */
export function SignIn(): React.ReactElement {
  const { login, completeTotp, isBusy } = useAuth();
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
                ? 'Use the account your administrator gave you.'
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
                    autoComplete="username"
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
