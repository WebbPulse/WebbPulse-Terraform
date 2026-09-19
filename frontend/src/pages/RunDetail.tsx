/** The run detail page: state, plan counts, the log tail and the three actions. */

import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  canCancel,
  canConfirm,
  canDiscard,
  defaultPhase,
  hasApplyPhase,
  isActive,
  type Run,
  type RunPhase,
} from '../api';
import {
  Button,
  ErrorNotice,
  PageHeader,
  RunLogViewer,
  Spinner,
  StateBadge,
  Tabs,
  formatDateTime,
  formatRelative,
  shortRunId,
} from '../components';

/** The run detail page. */
export function RunDetail(): React.ReactElement {
  const { runId = '' } = useParams<{ runId: string }>();
  const auth = useQueryAuth();
  const [phase, setPhase] = useState<RunPhase | null>(null);
  const query = usePolledQuery<Run>(
    ({ signal }) => api.getRun(runId, { signal }),
    {
      intervalMs: 5000,
      queryKey: `run:${runId}`,
      auth,
      enabled: runId !== '',
    }
  );
  const run = query.data;

  useEffect(() => {
    if (run !== null && phase === null) {
      setPhase(defaultPhase(run.status));
    }
  }, [run, phase]);

  if (query.isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-text-faint">
        <Spinner label="Loading the run" className="size-4" />
        Loading the run
      </div>
    );
  }
  if (run === null) {
    return <ErrorNotice error={query.error ?? new Error('Run not found.')} />;
  }

  const shownPhase = phase ?? defaultPhase(run.status);

  return (
    <div className="space-y-5">
      <PageHeader
        crumbs={[
          { label: 'Runs', to: '/runs' },
          { label: run.workspace_id, to: `/workspaces/${run.workspace_id}` },
        ]}
        title={`Run ${shortRunId(run.run_id)}`}
        meta={<StateBadge state={run.status} />}
        description={
          run.message === undefined || run.message === ''
            ? `${run.plan_only ? 'Plan only' : 'Plan and apply'}, created ${formatRelative(run.created_at)}.`
            : run.message
        }
        actions={
          <RunActions
            run={run}
            onDone={() => {
              void query.refetch();
            }}
          />
        }
      />

      <ErrorNotice error={query.error} />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_16rem]">
        <div className="min-w-0 space-y-4">
          <PlanSummary run={run} />
          {run.error === undefined ||
          run.error === null ||
          run.error === '' ? null : (
            <ErrorNotice error={new Error(run.error)} />
          )}
          <RunLogViewer
            runId={run.run_id}
            phase={shownPhase}
            live={isActive(run.status)}
            controls={
              <Tabs
                label="Run phase"
                className="border-b-0"
                tabs={[
                  { id: 'plan', label: 'Plan log' },
                  {
                    id: 'apply',
                    label: 'Apply log',
                    disabled: !hasApplyPhase(run.status),
                  },
                ]}
                value={shownPhase}
                onChange={setPhase}
              />
            }
          />
        </div>
        <PropertiesRail run={run} />
      </div>
    </div>
  );
}

/** The plan's resource counts, or a sentence when the plan has not reported. */
function PlanSummary({ run }: { run: Run }): React.ReactElement {
  if (run.changes === null || run.changes === undefined) {
    return (
      <p className="rounded-lg border border-dashed border-line px-4 py-3 text-sm text-text-faint">
        No plan summary yet.
      </p>
    );
  }
  const { add, change, destroy } = run.changes;
  const counts = [
    { label: 'To add', value: add, className: 'text-emerald-300' },
    { label: 'To change', value: change, className: 'text-amber-300' },
    { label: 'To destroy', value: destroy, className: 'text-rose-300' },
  ];
  return (
    <dl
      data-testid="plan-summary"
      aria-label="Plan summary"
      className="grid grid-cols-3 divide-x divide-line rounded-lg border border-line bg-panel"
    >
      {counts.map((count) => (
        <div key={count.label} className="px-4 py-3">
          <dt className="text-xs text-text-faint">{count.label}</dt>
          <dd
            className={`mt-0.5 font-mono text-xl tabular-nums ${count.className}`}
          >
            {count.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** One row of the properties rail. */
function Property({
  label,
  children,
  mono = false,
}: {
  label: string;
  children: React.ReactNode;
  mono?: boolean;
}): React.ReactElement {
  return (
    <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-2 py-1.5 text-xs">
      <dt className="text-text-faint">{label}</dt>
      <dd className={`min-w-0 break-all text-text ${mono ? 'font-mono' : ''}`}>
        {children}
      </dd>
    </div>
  );
}

/** A timestamp property, relative with the exact time on hover. */
function When({
  label,
  iso,
}: {
  label: string;
  iso: string | null | undefined;
}): React.ReactElement {
  return (
    <Property label={label}>
      {iso === null || iso === undefined ? (
        <span className="text-text-faint">-</span>
      ) : (
        <span title={formatDateTime(iso)}>{formatRelative(iso)}</span>
      )}
    </Property>
  );
}

/** The run's identifiers and timestamps, in a rail beside the log. */
function PropertiesRail({ run }: { run: Run }): React.ReactElement {
  return (
    <aside className="lg:sticky lg:top-6 lg:self-start">
      <h2 className="text-xs font-medium tracking-wide text-text-faint uppercase">
        Properties
      </h2>
      <dl className="mt-2 divide-y divide-line border-y border-line">
        <Property label="Workspace" mono>
          <Link
            to={`/workspaces/${run.workspace_id}`}
            className="text-brand-300 hover:text-brand-200"
          >
            {run.workspace_id}
          </Link>
        </Property>
        <Property label="Run id" mono>
          {run.run_id}
        </Property>
        <Property label="Configuration" mono>
          {run.config_version_id}
        </Property>
        <Property label="Mode">
          {run.plan_only ? 'Plan only' : 'Plan and apply'}
        </Property>
        {run.queued_behind === undefined ||
        run.queued_behind === null ? null : (
          <Property label="Queued behind" mono>
            {run.queued_behind}
          </Property>
        )}
        <When label="Created" iso={run.created_at} />
        <When label="Started" iso={run.started_at} />
        <When label="Finished" iso={run.finished_at} />
      </dl>
    </aside>
  );
}

/**
 * The confirm, cancel and discard buttons, each shown only in the states the
 * gating module allows.
 *
 * Confirm is not step-up gated: `@webbpulse/auth` exposes `stepUp` only as a
 * TOTP or recovery code exchange, with no passkey ceremony, so a passkey
 * step-up would have to be written here rather than reused. Reported as a gap.
 */
function RunActions({
  run,
  onDone,
}: {
  run: Run;
  onDone: () => void;
}): React.ReactElement | null {
  const [busy, setBusy] = useState<'confirm' | 'cancel' | 'discard' | null>(
    null
  );
  const [error, setError] = useState<unknown>(null);

  const allowConfirm = canConfirm(run);
  const allowCancel = canCancel(run);
  const allowDiscard = canDiscard(run);

  if (!allowConfirm && !allowCancel && !allowDiscard) {
    return null;
  }

  const act = async (
    action: 'confirm' | 'cancel' | 'discard'
  ): Promise<void> => {
    setBusy(action);
    setError(null);
    try {
      if (action === 'confirm') {
        await api.confirmRun(run.run_id);
      } else if (action === 'cancel') {
        await api.cancelRun(run.run_id);
      } else {
        await api.discardRun(run.run_id);
      }
      onDone();
    } catch (thrown) {
      setError(thrown);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col items-end gap-2">
      <div className="flex flex-wrap items-center gap-2">
        {allowCancel ? (
          <Button
            variant="danger"
            disabled={busy !== null}
            busy={busy === 'cancel'}
            busyLabel="Working"
            onClick={() => {
              void act('cancel');
            }}
          >
            Cancel run
          </Button>
        ) : null}
        {allowDiscard ? (
          <Button
            disabled={busy !== null}
            busy={busy === 'discard'}
            busyLabel="Working"
            onClick={() => {
              void act('discard');
            }}
          >
            Discard
          </Button>
        ) : null}
        {allowConfirm ? (
          <Button
            variant="primary"
            disabled={busy !== null}
            busy={busy === 'confirm'}
            busyLabel="Working"
            onClick={() => {
              void act('confirm');
            }}
          >
            Confirm and apply
          </Button>
        ) : null}
      </div>
      <ErrorNotice error={error} />
    </div>
  );
}
