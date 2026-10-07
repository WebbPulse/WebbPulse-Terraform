/** The one "Confirm it is you" prompt every step-up gated action goes through. */

import { useEffect, useState, type ReactNode } from 'react';
import { passkeysSupported } from '@webbpulse/auth';
import { useStepUp, type StepUpFailure } from '@webbpulse/auth/react';

import { api as defaultApi, type TerraformApi } from '../api';
import { Button } from './Button';
import { Dialog } from './Dialog';
import { Field, INPUT_CLASS } from './Field';
import { SegmentedControl, type Segment } from './SegmentedControl';

/** Props for {@link StepUpBoundary}. */
export interface StepUpBoundaryProps {
  children: ReactNode;
  /** The API whose gated calls prompt here. Injected for tests. */
  api?: TerraformApi;
}

/**
 * Mounts the step-up gate for the whole signed-in app.
 *
 * Sensitive actions (API keys, terraform login approval, deleting a workspace, its AWS
 * connection and sensitive variables, run token scopes, turning on auto-apply, GitHub App
 * settings and the module registry) need a sign-in from the last few minutes. Confirming a
 * run does not. When the server refuses one with `STEP_UP_REQUIRED`, the call is parked,
 * this prompt asks for a passkey, an authenticator code or the password, and the call is
 * sent once more. Nobody leaves the page. Cancelling rejects the call, so the page that made
 * it shows that nothing changed.
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
        <ConfirmIdentityDialog
          maxAge={gate.maxAge}
          error={stepUpErrorMessage(gate.error)}
          busy={gate.pending}
          onSubmit={(method, value) =>
            gate.submit(
              method === 'code' ? { code: value } : { password: value }
            )
          }
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

/** The typed ways to confirm, besides a passkey. */
export type StepUpFactor = 'code' | 'password';

const FACTORS: readonly Segment<StepUpFactor>[] = [
  { id: 'code', label: 'Authenticator code' },
  { id: 'password', label: 'Password' },
];

/** The localStorage key the last typed factor is remembered under, per browser. */
export const STEP_UP_FACTOR_KEY = 'webbpulse-terraform.step-up-factor';

/** The typed factor this browser last confirmed with, defaulting to the authenticator. */
export function rememberedFactor(): StepUpFactor {
  try {
    const stored = globalThis.localStorage.getItem(STEP_UP_FACTOR_KEY);
    return stored === 'password' ? 'password' : 'code';
  } catch {
    return 'code';
  }
}

/** Remembers the typed factor that just worked, ignoring a browser that refuses storage. */
function rememberFactor(factor: StepUpFactor): void {
  try {
    globalThis.localStorage.setItem(STEP_UP_FACTOR_KEY, factor);
  } catch {
    return;
  }
}

/** Props for {@link ConfirmIdentityDialog}. */
export interface ConfirmIdentityDialogProps {
  maxAge: number | null;
  error: string | null;
  busy: boolean;
  /** Confirms with a typed factor, resolving true once the call is on its way again. */
  onSubmit: (factor: StepUpFactor, value: string) => Promise<boolean>;
  /** Confirms with a passkey instead, or null where this browser cannot. */
  onPasskey?: (() => Promise<boolean>) | null;
  onCancel: () => void;
}

/**
 * The prompt itself: a passkey first where the browser has one, then an authenticator
 * or recovery code, or the password, with a cancel and a confirm.
 */
export function ConfirmIdentityDialog({
  maxAge,
  error,
  busy,
  onSubmit,
  onPasskey = null,
  onCancel,
}: ConfirmIdentityDialogProps): React.ReactElement {
  const [factor, setFactor] = useState<StepUpFactor>(rememberedFactor);
  const [value, setValue] = useState('');

  const submit = async (): Promise<void> => {
    const ok = await onSubmit(factor, value.trim());
    if (ok) {
      rememberFactor(factor);
    } else {
      setValue('');
    }
  };

  const isCode = factor === 'code';

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
      <div className="space-y-4">
        {onPasskey === null ? null : (
          <>
            <Button
              variant="primary"
              className="w-full"
              disabled={busy}
              onClick={() => void onPasskey()}
            >
              Use a passkey
            </Button>
            <div
              aria-hidden="true"
              className="flex items-center gap-3 text-xs text-text-faint"
            >
              <span className="h-px flex-1 bg-line" />
              or
              <span className="h-px flex-1 bg-line" />
            </div>
          </>
        )}
        <SegmentedControl
          label="Confirm with"
          segments={FACTORS}
          value={factor}
          onChange={(next) => {
            setFactor(next);
            setValue('');
          }}
        />
        <form
          aria-label="Confirm it is you"
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            if (value.trim() !== '') {
              void submit();
            }
          }}
        >
          <Field
            label={isCode ? 'Authenticator code' : 'Password'}
            hint={
              isCode
                ? 'The 6 digit code from your authenticator app, or a recovery code.'
                : undefined
            }
            error={error}
          >
            {(control) => (
              <input
                {...control}
                key={factor}
                type={isCode ? 'text' : 'password'}
                name={isCode ? 'code' : 'password'}
                autoComplete={isCode ? 'one-time-code' : 'current-password'}
                inputMode={isCode ? 'numeric' : undefined}
                spellCheck={false}
                data-autofocus={onPasskey === null ? true : undefined}
                required
                value={value}
                onChange={(event) => {
                  setValue(event.target.value);
                }}
                className={INPUT_CLASS}
              />
            )}
          </Field>
          <div className="flex items-center justify-end gap-2 pt-1">
            <Button variant="ghost" disabled={busy} onClick={onCancel}>
              Cancel
            </Button>
            <Button
              type="submit"
              variant={onPasskey === null ? 'primary' : 'secondary'}
              busy={busy}
              busyLabel={
                isCode ? 'Checking your code' : 'Confirming your password'
              }
              disabled={value.trim() === ''}
            >
              {isCode ? 'Confirm code' : 'Confirm password'}
            </Button>
          </div>
        </form>
      </div>
    </Dialog>
  );
}
