/** The one button, in the few variants the app needs. */

import type { ButtonHTMLAttributes, ReactNode } from 'react';

import { Spinner } from './Spinner';

/** How a button reads: the primary action, a secondary one, a quiet one, a destructive one, or a quiet one on a dark code block. */
export type ButtonVariant =
  'primary' | 'secondary' | 'ghost' | 'danger' | 'inverse';

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
    'bg-accent text-white shadow-xs hover:bg-accent-hover focus-visible:ring-accent',
  secondary:
    'border border-line-strong bg-panel text-text-strong shadow-xs hover:bg-raised focus-visible:ring-accent',
  ghost:
    'text-text hover:bg-raised hover:text-text-strong focus-visible:ring-accent',
  danger:
    'border border-danger-line bg-panel text-danger shadow-xs hover:bg-danger-soft focus-visible:ring-danger',
  inverse:
    'text-code-muted hover:bg-white/10 hover:text-code-text focus-visible:ring-code-text',
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
      className={`inline-flex shrink-0 items-center justify-center gap-2 rounded-md font-medium whitespace-nowrap transition-colors focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-bg focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 ${VARIANT_CLASSES[variant]} ${SIZE_CLASSES[size]} ${className}`}
      {...rest}
    >
      {busy ? <Spinner label={busyLabel} className="size-3.5" /> : null}
      {children}
    </button>
  );
}
