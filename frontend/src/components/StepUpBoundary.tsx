/** The one "Confirm it is you" prompt every step-up gated action goes through. */

import { useEffect, useState, type ReactNode } from 'react';
import { passkeysSupported } from '@webbpulse/auth';
import { useStepUp, type StepUpFailure } from '@webbpulse/auth/react';

import { api as defaultApi, type TerraformApi } from '../api';
import { Button } from './Button';
import { Dialog } from './Dialog';
import { Field, INPUT_CLASS } from './Field';

/** Props for {@link StepUpBoundary}. */
export interface StepUpBoundaryProps {
  children: ReactNode;
  /** The API whose gated calls prompt here. Injected for tests. */
  api?: TerraformApi;
}

/**
 * Mounts the step-up gate for the whole signed-in app.
 *
 * Sensitive actions (API keys, deleting a workspace, its AWS connection and
 * sensitive variables, confirming a run, turning on auto-apply, GitHub App settings and the module
 * registry) need a sign-in from the last few minutes. When the server refuses
 * one with `STEP_UP_REQUIRED`, the call is parked, this prompt asks for the
 * password or a passkey, and the call is sent once more. Cancelling rejects the call, so the
 * page that made it shows that nothing changed.
 */
export function StepUpBoundary({
  children,
  api = defaultApi,
}: StepUpBoundaryProps): React.ReactElement {
  const gate = useStepUp();
  const { withStepUp } = gate;

  useEffect(() => api.setStepUpGate(withStepUp), [api, withStepUp]);

  return (
    <>
      {children}
      {gate.open ? (
        <ConfirmPasswordDialog
          maxAge={gate.maxAge}
          error={stepUpErrorMessage(gate.error)}
          busy={gate.pending}
          onSubmit={(password) => gate.submit({ password })}
          onPasskey={
            passkeysSupported() ? () => gate.submit({ passkey: true }) : null
          }
          onCancel={gate.cancel}
        />
      ) : null}
    </>
  );
}

/** The sentence for a failed confirmation, or null when there is nothing to say. */
export function stepUpErrorMessage(error: StepUpFailure | null): string | null {
  if (error === null) {
    return null;
  }
  if ('reason' in error && error.reason === 'cancelled') {
    return null;
  }
  return error.message;
}

/** How long a confirmation lasts, in words, from the challenge's `max_age`. */
export function confirmationWindow(maxAge: number | null): string {
  if (maxAge === null || maxAge <= 0) {
    return 'a few minutes';
  }
  const minutes = Math.max(1, Math.round(maxAge / 60));
  return minutes === 1 ? '1 minute' : `${String(minutes)} minutes`;
}

/** Props for {@link ConfirmPasswordDialog}. */
export interface ConfirmPasswordDialogProps {
  maxAge: number | null;
  error: string | null;
  busy: boolean;
  onSubmit: (password: string) => Promise<boolean>;
  /** Confirms with a passkey instead, or null where this browser cannot. */
  onPasskey?: (() => Promise<boolean>) | null;
  onCancel: () => void;
}

/** The prompt itself: a password field, a passkey option, a cancel and a confirm. */
export function ConfirmPasswordDialog({
  maxAge,
  error,
  busy,
  onSubmit,
  onPasskey = null,
  onCancel,
}: ConfirmPasswordDialogProps): React.ReactElement {
  const [password, setPassword] = useState('');

  const submit = async (): Promise<void> => {
    const ok = await onSubmit(password);
    if (!ok) {
      setPassword('');
    }
  };

  return (
    <Dialog
      open
      onClose={() => {
        if (!busy) {
          onCancel();
        }
      }}
      title="Confirm it is you"
      description={`This action needs a recent sign-in. You will not be asked again for ${confirmationWindow(maxAge)}.`}
    >
      <form
        aria-label="Confirm it is you"
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (password !== '') {
            void submit();
          }
        }}
      >
        <Field label="Password" error={error}>
          {(control) => (
            <input
              {...control}
              type="password"
              name="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
              }}
              className={INPUT_CLASS}
            />
          )}
        </Field>
        <div className="flex items-center justify-end gap-2 pt-1">
          {onPasskey === null ? null : (
            <Button
              className="mr-auto"
              disabled={busy}
              onClick={() => void onPasskey()}
            >
              Use a passkey
            </Button>
          )}
          <Button variant="ghost" disabled={busy} onClick={onCancel}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="primary"
            busy={busy}
            busyLabel="Confirming your password"
            disabled={password === ''}
          >
            Confirm password
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
