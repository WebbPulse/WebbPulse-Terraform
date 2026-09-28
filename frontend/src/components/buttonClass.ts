/** The classes a button carries, shared with links that have to look like one. */

/** How a button reads: the primary action, a secondary one, a quiet one, a destructive one, or a quiet one on a dark code block. */
export type ButtonVariant =
  'primary' | 'secondary' | 'ghost' | 'danger' | 'inverse';

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary:
    'bg-accent text-accent-contrast shadow-xs hover:bg-accent-hover focus-visible:ring-accent',
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

/** The classes a button of one variant and size carries, for a link that has to look like one. */
export function buttonClass(
  variant: ButtonVariant = 'secondary',
  size: 'sm' | 'md' = 'md'
): string {
  return `inline-flex shrink-0 items-center justify-center gap-2 rounded-md font-medium whitespace-nowrap transition-colors focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-bg focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50 ${VARIANT_CLASSES[variant]} ${SIZE_CLASSES[size]}`;
}
