/** The product mark and wordmark. */

/** Props for {@link BrandMark}. */
export interface BrandMarkProps {
  className?: string;
}

/**
 * The mark: a pulse crossing three stacked layers.
 *
 * The layers stand for the infrastructure a workspace manages and the line for
 * the run passing through it. It is drawn in `currentColor` and one accent, so
 * it sits on either theme without a second file.
 */
export function BrandMark({
  className = 'size-5',
}: BrandMarkProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      role="img"
      aria-label="WebbPulse Terraform"
      className={className}
      fill="none"
    >
      <rect width="24" height="24" rx="6" className="fill-accent" />
      <path
        d="M4.5 8.25 12 4.5l7.5 3.75"
        stroke="var(--color-accent-contrast)"
        strokeOpacity="0.55"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M4.5 15.75 12 19.5l7.5-3.75"
        stroke="var(--color-accent-contrast)"
        strokeOpacity="0.55"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M4.5 12h3.25l1.75-3 2.5 6 1.75-3h6.25"
        stroke="var(--color-accent-contrast)"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Props for {@link Wordmark}. */
export interface WordmarkProps {
  /** Drops "WebbPulse" and shows only "Terraform", for a tight rail. */
  short?: boolean;
  className?: string;
}

/**
 * The mark beside the product name.
 *
 * "WebbPulse" is set in the muted weight and "Terraform" in the strong one, so
 * the product reads first and the owner second.
 */
export function Wordmark({
  short = false,
  className = '',
}: WordmarkProps): React.ReactElement {
  return (
    <span className={`inline-flex items-center gap-2 ${className}`}>
      <BrandMark />
      <span className="text-sm leading-none whitespace-nowrap">
        {short ? null : (
          <span className="font-medium text-text-muted">WebbPulse </span>
        )}
        <span className="font-semibold text-text-strong">Terraform</span>
      </span>
    </span>
  );
}
