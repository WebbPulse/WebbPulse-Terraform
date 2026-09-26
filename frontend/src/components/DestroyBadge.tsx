/** The tag that marks a destroy run wherever runs are listed or shown. */

/** Props for {@link DestroyBadge}. */
export interface DestroyBadgeProps {
  className?: string;
}

/** A small danger-toned "Destroy" tag. */
export function DestroyBadge({
  className = '',
}: DestroyBadgeProps): React.ReactElement {
  return (
    <span
      data-testid="destroy-badge"
      className={`inline-flex shrink-0 items-center rounded-full border border-danger-line bg-danger-soft px-2 py-0.5 text-[11px] font-medium text-destroy ${className}`}
    >
      Destroy
    </span>
  );
}
