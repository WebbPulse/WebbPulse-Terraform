/** The sign-in page: a password leg, then a TOTP leg when one is asked for. */

import { useState } from 'react';
import { useAuth } from '@webbpulse/auth/react';

import { ErrorNotice, Spinner } from '../components';

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
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm space-y-4">
        <h1 className="text-xl font-semibold text-surface-50">
          WebbPulse Terraform
        </h1>
        <ErrorNotice error={error} />
        {ticket === null ? (
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              void submitPassword();
            }}
          >
            <label className="block text-sm">
              <span className="text-surface-300">Email</span>
              <input
                type="email"
                required
                autoComplete="username"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                }}
                className="mt-1 w-full rounded-md border border-surface-600 bg-surface-800 px-3 py-2"
              />
            </label>
            <label className="block text-sm">
              <span className="text-surface-300">Password</span>
              <input
                type="password"
                required
                autoComplete="current-password"
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value);
                }}
                className="mt-1 w-full rounded-md border border-surface-600 bg-surface-800 px-3 py-2"
              />
            </label>
            <button
              type="submit"
              disabled={isBusy}
              className="flex w-full items-center justify-center gap-2 rounded-md bg-brand-600 px-3 py-2 font-medium text-white disabled:opacity-60"
            >
              {isBusy ? <Spinner label="Signing in" /> : null}
              Sign in
            </button>
          </form>
        ) : (
          <form
            className="space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              void submitCode();
            }}
          >
            <label className="block text-sm">
              <span className="text-surface-300">
                Authentication or recovery code
              </span>
              <input
                inputMode="numeric"
                required
                autoComplete="one-time-code"
                value={code}
                onChange={(event) => {
                  setCode(event.target.value);
                }}
                className="mt-1 w-full rounded-md border border-surface-600 bg-surface-800 px-3 py-2 font-mono"
              />
            </label>
            <button
              type="submit"
              disabled={isBusy}
              className="flex w-full items-center justify-center gap-2 rounded-md bg-brand-600 px-3 py-2 font-medium text-white disabled:opacity-60"
            >
              {isBusy ? <Spinner label="Verifying" /> : null}
              Verify
            </button>
            <button
              type="button"
              onClick={() => {
                setTicket(null);
                setCode('');
                setError(null);
              }}
              className="w-full rounded-md border border-surface-600 px-3 py-2 text-sm text-surface-200"
            >
              Back
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
