/** The one button, in the few variants the app needs. */

import type { ButtonHTMLAttributes, ReactNode } from 'react';

import { Spinner } from './Spinner';

/** How a button reads: the primary action, a secondary one, a quiet one, or a destructive one. */
export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';

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

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    'bg-brand-600 text-white hover:bg-brand-500 focus-visible:ring-brand-400',
  secondary:
    'border border-surface-600 bg-surface-800 text-surface-100 hover:border-surface-500 hover:bg-surface-700 focus-visible:ring-brand-400',
  ghost:
    'text-surface-200 hover:bg-surface-800 hover:text-surface-50 focus-visible:ring-brand-400',
  danger:
    'border border-rose-700/60 bg-rose-950/40 text-rose-200 hover:bg-rose-900/50 focus-visible:ring-rose-400',
};

const SIZE_CLASSES = {
  sm: 'h-7 px-2.5 text-xs',
  md: 'h-8 px-3 text-sm',
};

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
      className={`inline-flex shrink-0 items-center justify-center gap-2 rounded-md font-medium whitespace-nowrap transition-colors focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-surface-900 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 ${VARIANT_CLASSES[variant]} ${SIZE_CLASSES[size]} ${className}`}
      {...rest}
    >
      {busy ? <Spinner label={busyLabel} className="size-3.5" /> : null}
      {children}
    </button>
  );
}
