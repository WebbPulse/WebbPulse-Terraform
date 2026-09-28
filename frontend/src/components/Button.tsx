/** The one button, in the few variants the app needs. */

import type { ButtonHTMLAttributes, ReactNode } from 'react';

import { buttonClass, type ButtonVariant } from './buttonClass';
import { Spinner } from './Spinner';

export type { ButtonVariant } from './buttonClass';

/** Props for {@link Button}. */
export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: 'sm' | 'md';
  /** Shows a spinner and disables the button while a request is in flight. */
  busy?: boolean;
  /** The spinner's accessible label while busy. */
  busyLabel?: string;
  children: ReactNode;
}

/** A button with consistent sizing, focus ring and busy state. */
export function Button({
  variant = 'secondary',
  size = 'md',
  busy = false,
  busyLabel = 'Working',
  disabled,
  className = '',
  children,
  type = 'button',
  ...rest
}: ButtonProps): React.ReactElement {
  return (
    <button
      type={type}
      disabled={disabled === true || busy}
      className={`${buttonClass(variant, size)} ${className}`}
      {...rest}
    >
      {busy ? <Spinner label={busyLabel} className="size-3.5" /> : null}
      {children}
    </button>
  );
}
