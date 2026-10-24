/** Organization settings for the audit log: who changed what, filtered, paged and exported as CSV. */

import { useId, useState } from 'react';
import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  type AuditEvent,
  type AuditEventList,
  type AuditEventQuery,
  type WorkspaceList,
} from '../../api';
import {
  Button,
  EmptyState,
  ErrorNotice,
  formatDateTime,
  INPUT_CLASS,
  PageHeader,
  RelativeTime,
} from '../../components';
import { RowsSkeleton } from './Skeleton';

/** The page's crumbs, the page itself left to the title. */
const CRUMBS = [{ label: 'Settings' }] as const;

/** How many events one page shows. */
export const PAGE_SIZE = 50;

/** The time ranges offered, in days back from now. */
export const RANGES: readonly { days: number; label: string }[] = [
  { days: 1, label: 'Last 24 hours' },
  { days: 7, label: 'Last 7 days' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
  { days: 365, label: 'Last year' },
];

/** The range a fresh visit starts with. */
export const DEFAULT_RANGE_DAYS = 30;

/** The filters the page holds. Empty strings mean unfiltered. */
export interface AuditFilters {
  action: string;
  actorId: string;
  workspaceId: string;
  days: number;
}

/** The API query for the filters, the range anchored at `now`. */
export function auditQuery(
  filters: AuditFilters,
  now: number = Date.now()
): AuditEventQuery {
  const query: AuditEventQuery = {
    since: new Date(now - filters.days * 86_400_000).toISOString(),
  };
  if (filters.action !== '') {
    query.action = filters.action;
  }
  if (filters.actorId !== '') {
    query.actor_id = filters.actorId;
  }
  if (filters.workspaceId !== '') {
    query.target_type = 'workspace';
    query.target_id = filters.workspaceId;
  }
  return query;
}

/** A recorded value as one short line of text. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === '') {
    return 'none';
  }
  if (Array.isArray(value)) {
    return value.length === 0 ? 'none' : value.map(formatValue).join(', ');
  }
  if (
    typeof value === 'string' ||
    typeof value === 'number' ||
    typeof value === 'boolean'
  ) {
    return String(value);
  }
  return JSON.stringify(value);
}

/** One field an event recorded: an edit's before and after, or a payload value. */
export type FieldChange =
  | { kind: 'edit'; field: string; before: unknown; after: unknown }
  | { kind: 'value'; field: string; value: unknown };

/** Whether two recorded values are the same. */
function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/** What an event changed: an edit's fields whose value moved, the payload otherwise. */
export function changedFields(event: AuditEvent): FieldChange[] {
  const before = event.before ?? null;
  const after = event.after ?? null;
  if (before !== null || after !== null) {
    return Object.keys({ ...before, ...after })
      .filter((field) => !sameValue(before?.[field], after?.[field]))
      .map((field): FieldChange => ({
        kind: 'edit',
        field,
        before: before?.[field],
        after: after?.[field],
      }));
  }
  return Object.entries(event.payload).map(([field, value]): FieldChange => ({
    kind: 'value',
    field,
    value,
  }));
}

/** A change as one short line: `field: before -> after`, or `field: value`. */
export function describeChange(change: FieldChange): string {
  return change.kind === 'edit'
    ? `${change.field}: ${formatValue(change.before)} -> ${formatValue(change.after)}`
    : `${change.field}: ${formatValue(change.value)}`;
}

/** A recorded value in full, objects pretty printed, for the detail view. */
export function formatDetail(value: unknown): string {
  if (
    value !== null &&
    typeof value === 'object' &&
    !(Array.isArray(value) && value.every((item) => typeof item !== 'object'))
  ) {
    return JSON.stringify(value, null, 2);
  }
  return formatValue(value);
}

/** Who made a change, as a row names them. */
export function actorLabel(event: AuditEvent): string {
  if (event.actor_kind === 'run_token') {
    return 'A run';
  }
  return event.actor_name ?? event.actor_id;
}

/** An action label lowered to sit mid sentence, an acronym such as API kept. */
export function verbPhrase(label: string): string {
  if (/^[A-Z]{2}/.test(label)) {
    return label;
  }
  return label.charAt(0).toLowerCase() + label.slice(1);
}

/** What an event acted on, by name where one is known. */
export function targetLabel(
  event: AuditEvent,
  workspaceNames: Map<string, string>
): string {
  if (event.target_label !== '') {
    return event.target_label;
  }
  return workspaceNames.get(event.target_id) ?? event.target_id;
}

/** One person or key the actor filter offers. */
export interface ActorOption {
  id: string;
  label: string;
}

/** The actors held so far merged with those on the shown events, runs left out, sorted by name. */
export function mergeActors(
  held: readonly ActorOption[],
  events: readonly AuditEvent[]
): ActorOption[] {
  const byId = new Map(held.map((actor) => [actor.id, actor]));
  for (const event of events) {
    if (event.actor_kind !== 'run_token' && !byId.has(event.actor_id)) {
      byId.set(event.actor_id, {
        id: event.actor_id,
        label: actorLabel(event),
      });
    }
  }
  return [...byId.values()].sort((a, b) => a.label.localeCompare(b.label));
}

/** How the actor reached the plane, in words. */
const KIND_LABELS: Record<string, string> = {
  user: 'Session',
  api_key: 'API key',
  run_token: 'Run token',
};

/** Where a request came from, in words. */
const SOURCE_LABELS: Record<string, string> = {
  web: 'web',
  api: 'API',
  cli: 'CLI',
};

/** Merges a later page under the held events, dropping any the earlier page already showed. */
export function appendPage(
  held: AuditEvent[],
  page: AuditEvent[]
): AuditEvent[] {
  const seen = new Set(held.map((event) => event.event_id));
  return [...held, ...page.filter((event) => !seen.has(event.event_id))];
}

/** Hands the browser a CSV file to save. */
function download(body: string, filename: string): void {
  const url = URL.createObjectURL(
    new Blob([body], { type: 'text/csv;charset=utf-8' })
  );
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** The audit log page. */
export function Audit(): React.ReactElement {
  const auth = useQueryAuth();
  const [filters, setFilters] = useState<AuditFilters>({
    action: '',
    actorId: '',
    workspaceId: '',
    days: DEFAULT_RANGE_DAYS,
  });
  const [older, setOlder] = useState<{
    items: AuditEvent[];
    cursor: string | null;
  } | null>(null);
  const [heldActors, setHeldActors] = useState<ActorOption[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);

  const events = usePolledQuery<AuditEventList>(
    ({ signal }) =>
      api.listAuditEvents(
        { ...auditQuery(filters), limit: PAGE_SIZE },
        { signal }
      ),
    {
      intervalMs: 60_000,
      queryKey: [
        'audit-events',
        filters.action,
        filters.actorId,
        filters.workspaceId,
        filters.days,
      ],
      auth,
    }
  );
  const workspaces = usePolledQuery<WorkspaceList>(
    ({ signal }) => api.listWorkspaces({ signal }),
    { intervalMs: 300_000, queryKey: 'audit-workspaces', auth }
  );

  const first = events.data?.items ?? [];
  const items = older === null ? first : appendPage(first, older.items);
  const cursor =
    older === null ? (events.data?.next_cursor ?? null) : older.cursor;
  const eventTypes = events.data?.event_types ?? [];
  const actors = mergeActors(heldActors, items);
  const workspaceNames = new Map(
    (workspaces.data?.items ?? []).map((workspace) => [
      workspace.workspace_id,
      workspace.name,
    ])
  );

  const change = (next: Partial<AuditFilters>): void => {
    setHeldActors(actors);
    setFilters((current) => ({ ...current, ...next }));
    setOlder(null);
    setActionError(null);
  };

  const loadMore = async (): Promise<void> => {
    if (cursor === null) {
      return;
    }
    setLoadingMore(true);
    setActionError(null);
    try {
      const page = await api.listAuditEvents({
        ...auditQuery(filters),
        limit: PAGE_SIZE,
        cursor,
      });
      setOlder({
        items: appendPage(older?.items ?? [], page.items),
        cursor: page.next_cursor ?? null,
      });
    } catch (error) {
      setActionError(error);
    } finally {
      setLoadingMore(false);
    }
  };

  const exportCsv = async (): Promise<void> => {
    setExporting(true);
    setActionError(null);
    try {
      const body = await api.exportAuditEvents(auditQuery(filters));
      download(body, 'audit-events.csv');
    } catch (error) {
      setActionError(error);
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Audit log"
        crumbs={CRUMBS}
        description="Every change to workspaces, their access settings, variables and API keys, every state download and every run decision. Kept for a year. Variable values and key secrets are never recorded."
        actions={
          <Button
            variant="secondary"
            busy={exporting}
            busyLabel="Exporting"
            onClick={() => {
              void exportCsv();
            }}
          >
            Export CSV
          </Button>
        }
      />
      <form
        aria-label="Filter the audit log"
        className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:flex lg:flex-wrap lg:items-end"
        onSubmit={(event) => {
          event.preventDefault();
        }}
      >
        <FilterSelect
          label="Action"
          value={filters.action}
          onChange={(action) => {
            change({ action });
          }}
          options={[
            { value: '', label: 'All actions' },
            ...eventTypes.map((type) => ({
              value: type.action,
              label: type.label,
            })),
          ]}
        />
        <FilterSelect
          label="Actor"
          value={filters.actorId}
          onChange={(actorId) => {
            change({ actorId });
          }}
          options={[
            { value: '', label: 'All actors' },
            ...actors.map((actor) => ({ value: actor.id, label: actor.label })),
          ]}
        />
        <FilterSelect
          label="Workspace"
          value={filters.workspaceId}
          onChange={(workspaceId) => {
            change({ workspaceId });
          }}
          options={[
            { value: '', label: 'All workspaces' },
            ...(workspaces.data?.items ?? []).map((workspace) => ({
              value: workspace.workspace_id,
              label: workspace.name,
            })),
          ]}
        />
        <FilterSelect
          label="Time range"
          value={String(filters.days)}
          onChange={(days) => {
            change({ days: Number(days) });
          }}
          options={RANGES.map((range) => ({
            value: String(range.days),
            label: range.label,
          }))}
        />
      </form>
      <ErrorNotice error={events.error ?? actionError} />
      {events.isLoading ? (
        <RowsSkeleton label="Loading the audit log" />
      ) : events.data === null ? null : items.length === 0 ? (
        <EmptyState
          title="No events in this range"
          hint="Changes show up here as soon as they land."
        />
      ) : (
        <div className="space-y-3">
          <EventList events={items} workspaceNames={workspaceNames} />
          {cursor === null ? null : (
            <div className="flex justify-center">
              <Button
                variant="ghost"
                size="sm"
                busy={loadingMore}
                busyLabel="Loading"
                onClick={() => {
                  void loadMore();
                }}
              >
                Load more
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** One labelled filter dropdown. */
function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: readonly { value: string; label: string }[];
}): React.ReactElement {
  return (
    <label className="flex min-w-0 flex-col gap-1 text-xs text-text-muted lg:w-48">
      {label}
      <select
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
        className={`${INPUT_CLASS} w-full min-w-0`}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

/** The events newest first, one line each, a click away from their detail. */
function EventList({
  events,
  workspaceNames,
}: {
  events: AuditEvent[];
  workspaceNames: Map<string, string>;
}): React.ReactElement {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const toggle = (eventId: string): void => {
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(eventId)) {
        next.delete(eventId);
      } else {
        next.add(eventId);
      }
      return next;
    });
  };
  return (
    <ul
      aria-label="Audit events"
      className="divide-y divide-line overflow-hidden rounded-lg border border-line bg-panel"
    >
      {events.map((event) => (
        <EventRow
          key={event.event_id}
          event={event}
          target={targetLabel(event, workspaceNames)}
          open={open.has(event.event_id)}
          onToggle={() => {
            toggle(event.event_id);
          }}
        />
      ))}
    </ul>
  );
}

/** One event as a sentence, actor, action, target and when, with its detail below while open. */
function EventRow({
  event,
  target,
  open,
  onToggle,
}: {
  event: AuditEvent;
  target: string;
  open: boolean;
  onToggle: () => void;
}): React.ReactElement {
  const detailId = useId();
  const changes = changedFields(event);
  const summary = changes.map(describeChange).join(' · ');
  return (
    <li>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={detailId}
        onClick={onToggle}
        className="flex w-full min-w-0 items-center gap-3 px-3 py-2.5 text-left text-sm transition-colors hover:bg-raised/60 focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none focus-visible:ring-inset sm:px-4"
      >
        <svg
          viewBox="0 0 16 16"
          aria-hidden="true"
          className={`size-3.5 shrink-0 text-text-faint transition-transform ${open ? 'rotate-90' : ''}`}
          fill="none"
        >
          <path
            d="m6 3 5 5-5 5"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span className="min-w-0 flex-1 truncate">
          <span className="font-medium text-text-strong">
            {actorLabel(event)}
          </span>{' '}
          <span className="text-text-muted">{verbPhrase(event.label)}</span>{' '}
          <span className="text-text">{target}</span>
          {summary === '' ? null : (
            <span className="hidden text-xs text-text-faint md:inline">
              {' '}
              · {summary}
            </span>
          )}
        </span>
        <RelativeTime
          iso={event.occurred_at}
          className="shrink-0 text-xs whitespace-nowrap text-text-muted"
        />
      </button>
      {open ? (
        <EventDetail
          id={detailId}
          event={event}
          target={target}
          changes={changes}
        />
      ) : null}
    </li>
  );
}

/** Everything an event recorded: what changed, in full, then who, from where and the ids. */
function EventDetail({
  id,
  event,
  target,
  changes,
}: {
  id: string;
  event: AuditEvent;
  target: string;
  changes: FieldChange[];
}): React.ReactElement {
  const facts: [string, string, boolean][] = [
    ['When', formatDateTime(event.occurred_at), false],
    ['Actor', actorLabel(event), false],
    [
      'Signed in with',
      [
        KIND_LABELS[event.actor_kind] ?? event.actor_kind,
        SOURCE_LABELS[event.source] ?? event.source,
        event.amr.join(', '),
      ]
        .filter((part) => part !== '')
        .join(' · '),
      false,
    ],
    ['IP address', event.ip === '' ? 'Not recorded' : event.ip, true],
    ['Actor ID', event.actor_id, true],
    ['Target', `${target} (${event.target_type})`, false],
    ['Target ID', event.target_id, true],
    ['Event ID', event.event_id, true],
  ];
  return (
    <div
      id={id}
      className="space-y-4 border-t border-line bg-bg/40 px-3 py-4 sm:px-4 sm:pl-10"
    >
      <section aria-label="Changes" className="space-y-2">
        <h3 className="text-xs font-medium text-text-muted">
          {changes.length === 0
            ? 'No fields changed'
            : changes[0]?.kind === 'edit'
              ? 'Changed fields'
              : 'Details'}
        </h3>
        {changes.length === 0 ? null : (
          <ul className="space-y-3">
            {changes.map((change) => (
              <li key={change.field} className="min-w-0 space-y-1">
                <div className="font-mono text-xs text-text-strong break-words">
                  {change.field}
                </div>
                {change.kind === 'edit' ? (
                  <div className="grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2">
                    <DetailValue label="Before" value={change.before} />
                    <DetailValue label="After" value={change.after} />
                  </div>
                ) : (
                  <DetailValue value={change.value} />
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
      <dl className="grid min-w-0 grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-[max-content_minmax(0,1fr)]">
        {facts.map(([term, value, mono]) => (
          <div key={term} className="contents">
            <dt className="text-xs text-text-muted sm:pt-0.5">{term}</dt>
            <dd
              className={`mb-1 min-w-0 break-words text-text sm:mb-0 ${mono ? 'font-mono text-xs sm:pt-0.5' : ''}`}
            >
              {value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** One recorded value in a box that wraps at word boundaries. */
function DetailValue({
  label,
  value,
}: {
  label?: string;
  value: unknown;
}): React.ReactElement {
  return (
    <div className="min-w-0 rounded-md border border-line bg-panel px-2.5 py-1.5">
      {label === undefined ? null : (
        <div className="text-[11px] text-text-faint">{label}</div>
      )}
      <pre className="font-mono text-xs whitespace-pre-wrap break-words text-text">
        {formatDetail(value)}
      </pre>
    </div>
  );
}
