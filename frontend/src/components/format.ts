/** Rendering helpers for dates and sizes. */

const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'short',
});

/** An ISO timestamp as a short local date and time, or the raw value if unparseable. */
export function formatDateTime(iso: string): string {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? iso : DATE_TIME.format(at);
}

/** How long ago an ISO timestamp was, in coarse words, or the date once it is old. */
export function formatRelative(iso: string, now: number = Date.now()): string {
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) {
    return iso;
  }
  const seconds = Math.round((now - at) / 1000);
  if (seconds < 45) {
    return 'just now';
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${String(minutes)} min ago`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return `${String(hours)} h ago`;
  }
  const days = Math.round(hours / 24);
  if (days < 7) {
    return `${String(days)} d ago`;
  }
  return formatDateTime(iso);
}

/** A byte count in the nearest unit. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${String(bytes)} B`;
  }
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit] ?? 'GB'}`;
}

/** The tail of a run id, enough to tell runs apart at a glance. */
export function shortRunId(runId: string): string {
  return runId.length > 12 ? runId.slice(-8) : runId;
}

/** A span of milliseconds in coarse words, such as "3 minutes" or "12 seconds". */
export function formatDuration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) {
    return `${String(seconds)} ${seconds === 1 ? 'second' : 'seconds'}`;
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${String(minutes)} ${minutes === 1 ? 'minute' : 'minutes'}`;
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  const hoursPart = `${String(hours)} ${hours === 1 ? 'hour' : 'hours'}`;
  return rest === 0 ? hoursPart : `${hoursPart} ${String(rest)} min`;
}

/** The milliseconds between two ISO timestamps, the second defaulting to now. */
export function elapsedBetween(
  startIso: string | null | undefined,
  endIso: string | null | undefined,
  now: number = Date.now()
): number | null {
  if (startIso === null || startIso === undefined) {
    return null;
  }
  const start = new Date(startIso).getTime();
  if (Number.isNaN(start)) {
    return null;
  }
  const end =
    endIso === null || endIso === undefined ? now : new Date(endIso).getTime();
  return Number.isNaN(end) ? null : Math.max(0, end - start);
}
