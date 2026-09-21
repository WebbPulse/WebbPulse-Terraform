/** The loading indicator every route guard and in-flight button shares. */

/** Props for {@link Spinner}. */
export interface SpinnerProps {
  /** The accessible label. Defaults to "Loading". */
  label?: string;
  className?: string;
}

/** A spinner, labelled for assistive technology. */
export function Spinner({
  label = 'Loading',
  className = '',
}: SpinnerProps): React.ReactElement {
  return (
    <span
      role="status"
      aria-label={label}
      className={`inline-block size-5 animate-spin rounded-full border-2 border-line-strong border-t-accent ${className}`}
    />
  );
}
