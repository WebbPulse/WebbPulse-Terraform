/** A relative timestamp that keeps itself current, on one clock shared by every instance. */

import { formatDateTime, formatRelative } from './format';
import { useClock } from './useClock';

/** Props for {@link RelativeTime}. */
export interface RelativeTimeProps {
  /** The ISO timestamp to describe. */
  iso: string;
  className?: string;
}

/**
 * The timestamp as "3 min ago", re-rendered as the clock moves.
 *
 * A value computed once at render goes stale the moment nothing else changes,
 * which is how "just now" ended up beside a minute long duration.
 */
export function RelativeTime({
  iso,
  className,
}: RelativeTimeProps): React.ReactElement {
  const current = useClock();
  return (
    <time dateTime={iso} title={formatDateTime(iso)} className={className}>
      {formatRelative(iso, current)}
    </time>
  );
}
