/** Organization settings for the audit log: who changed what, filtered, paged and exported as CSV. */

import { useState } from 'react';
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
  INPUT_CLASS,
  PageHeader,
  RelativeTime,
  Table,
  Td,
  Th,
  Tr,
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

/** What an event changed, one line per field: `before -> after` for an edit, the payload otherwise. */
export function describeChange(event: AuditEvent): string[] {
  const before = event.before ?? null;
  const after = event.after ?? null;
  if (before !== null || after !== null) {
    const fields = Object.keys({ ...before, ...after });
    return fields.map(
      (field) =>
        `${field}: ${formatValue(before?.[field])} -> ${formatValue(after?.[field])}`
    );
  }
  return Object.entries(event.payload).map(
    ([field, value]) => `${field}: ${formatValue(value)}`
  );
}

/** Who made a change, as the table names them. */
export function actorLabel(event: AuditEvent): string {
  if (event.actor_kind === 'run_token') {
    return `Run ${event.actor_id}`;
  }
  return event.actor_name ?? event.actor_id;
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
    workspaceId: '',
    days: DEFAULT_RANGE_DAYS,
  });
  const [older, setOlder] = useState<{
    items: AuditEvent[];
    cursor: string | null;
  } | null>(null);
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
  const workspaceNames = new Map(
    (workspaces.data?.items ?? []).map((workspace) => [
      workspace.workspace_id,
      workspace.name,
    ])
  );

  const change = (next: Partial<AuditFilters>): void => {
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
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
        }}
      >
        <label className="flex min-w-48 flex-1 flex-col gap-1 text-xs text-text-muted sm:flex-none">
          Action
          <select
            value={filters.action}
            onChange={(event) => {
              change({ action: event.target.value });
            }}
            className={INPUT_CLASS}
          >
            <option value="">All actions</option>
            {eventTypes.map((type) => (
              <option key={type.action} value={type.action}>
                {type.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-48 flex-1 flex-col gap-1 text-xs text-text-muted sm:flex-none">
          Workspace
          <select
            value={filters.workspaceId}
            onChange={(event) => {
              change({ workspaceId: event.target.value });
            }}
            className={INPUT_CLASS}
          >
            <option value="">All workspaces</option>
            {(workspaces.data?.items ?? []).map((workspace) => (
              <option
                key={workspace.workspace_id}
                value={workspace.workspace_id}
              >
                {workspace.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-40 flex-1 flex-col gap-1 text-xs text-text-muted sm:flex-none">
          Time range
          <select
            value={filters.days}
            onChange={(event) => {
              change({ days: Number(event.target.value) });
            }}
            className={INPUT_CLASS}
          >
            {RANGES.map((range) => (
              <option key={range.days} value={range.days}>
                {range.label}
              </option>
            ))}
          </select>
        </label>
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
          <EventTable events={items} workspaceNames={workspaceNames} />
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

/** The events, newest first. */
function EventTable({
  events,
  workspaceNames,
}: {
  events: AuditEvent[];
  workspaceNames: Map<string, string>;
}): React.ReactElement {
  return (
    <Table label="Audit events">
      <thead>
        <tr>
          <Th>Time</Th>
          <Th>Actor</Th>
          <Th>Action</Th>
          <Th>Target</Th>
          <Th>Details</Th>
        </tr>
      </thead>
      <tbody>
        {events.map((event) => {
          const target =
            event.target_label !== ''
              ? event.target_label
              : (workspaceNames.get(event.target_id) ?? event.target_id);
          const lines = describeChange(event);
          return (
            <Tr key={event.event_id}>
              <Td className="whitespace-nowrap text-text-muted">
                <RelativeTime iso={event.occurred_at} />
              </Td>
              <Td>
                <div className="font-medium text-text-strong">
                  {actorLabel(event)}
                </div>
                <div className="text-xs text-text-faint">
                  {[
                    KIND_LABELS[event.actor_kind] ?? event.actor_kind,
                    SOURCE_LABELS[event.source] ?? event.source,
                    event.ip,
                  ]
                    .filter((part) => part !== '')
                    .join(' · ')}
                </div>
              </Td>
              <Td className="whitespace-nowrap text-text">{event.label}</Td>
              <Td>
                <div className="text-text">{target}</div>
                <div className="font-mono text-xs text-text-faint">
                  {event.target_id}
                </div>
              </Td>
              <Td className="max-w-md">
                {lines.length === 0 ? (
                  <span className="text-text-faint">No details</span>
                ) : (
                  <ul className="space-y-0.5 font-mono text-xs break-all text-text-muted">
                    {lines.map((line) => (
                      <li key={line}>{line}</li>
                    ))}
                  </ul>
                )}
              </Td>
            </Tr>
          );
        })}
      </tbody>
    </Table>
  );
}
