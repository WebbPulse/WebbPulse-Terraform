/** The run page: its state, its metrics and its phases as expandable sections. */

import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { usePolledQuery } from '@webbpulse/api-client/react';
import { useQueryAuth } from '@webbpulse/auth/react';

import {
  api,
  applyPhaseStatus,
  canCancel,
  canConfirm,
  canDiscard,
  isActive,
  planPhaseStatus,
  type PhaseStatus,
  type Run,
  type RunTone,
} from '../api';
import {
  Button,
  Disclosure,
  ErrorNotice,
  PageHeader,
  RunLogViewer,
  Spinner,
  StateBadge,
  elapsedBetween,
  formatDateTime,
  formatDuration,
  formatRelative,
  shortRunId,
  useNow,
} from '../components';
import { useOptionalWorkspace } from './workspaceContext';

/** The three sections of the page. */
type SectionId = 'details' | 'plan' | 'apply';

/** Which sections open on their own for a run in this state. */
function defaultOpen(run: Run): Record<SectionId, boolean> {
  const apply = applyPhaseStatus(run);
  const applyBusy =
    apply === 'running' ||
    apply === 'finished' ||
    apply === 'errored' ||
    apply === 'cancelled';
  return {
    details: false,
    plan: !applyBusy,
    apply: apply !== null,
  };
}

/** The run page, inside a workspace or on its own. */
export function RunDetail(): React.ReactElement {
  const { runId = '' } = useParams<{ runId: string }>();
  const auth = useQueryAuth();
  const inWorkspace = useOptionalWorkspace() !== null;
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

  const body = (
    <RunBody
      run={run}
      inWorkspace={inWorkspace}
      onChanged={() => {
        void query.refetch();
      }}
    />
  );

  if (inWorkspace) {
    return (
      <div className="space-y-5">
        <ErrorNotice error={query.error} />
        {body}
      </div>
    );
  }
  return (
    <div className="space-y-5">
      <PageHeader
        crumbs={[
          { label: 'Runs', to: '/runs' },
          { label: run.workspace_id, to: `/workspaces/${run.workspace_id}` },
        ]}
        title={`Run ${shortRunId(run.run_id)}`}
        mono
      />
      <ErrorNotice error={query.error} />
      {body}
    </div>
  );
}

/** The title row, the metrics and the phase sections. */
function RunBody({
  run,
  inWorkspace,
  onChanged,
}: {
  run: Run;
  inWorkspace: boolean;
  onChanged: () => void;
}): React.ReactElement {
  const [overrides, setOverrides] = useState<
    Partial<Record<SectionId, boolean>>
  >({});
  const defaults = defaultOpen(run);
  const isOpen = (id: SectionId): boolean => overrides[id] ?? defaults[id];
  const toggle = (id: SectionId): void => {
    setOverrides((previous) => ({ ...previous, [id]: !isOpen(id) }));
  };
  const active = isActive(run.status);
  const now = useNow(active);
  const elapsed = elapsedBetween(run.started_at, run.finished_at, now);
  const plan = planPhaseStatus(run);
  const apply = applyPhaseStatus(run);
  const title =
    run.message === undefined || run.message === ''
      ? `Run ${shortRunId(run.run_id)}`
      : run.message;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          {inWorkspace ? (
            <nav
              aria-label="Run breadcrumb"
              className="text-xs text-text-faint"
            >
              <Link
                to={`/workspaces/${run.workspace_id}/runs`}
                className="hover:text-accent hover:underline"
              >
                Runs
              </Link>
              <span aria-hidden="true" className="mx-1.5 text-line-strong">
                /
              </span>
              <span className="font-mono">{shortRunId(run.run_id)}</span>
            </nav>
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold text-text-strong">
              {title}
            </h2>
            <StateBadge state={run.status} />
            {run.plan_only ? (
              <span className="rounded-full border border-line-strong px-2 py-0.5 text-[11px] text-text-muted">
                Plan only
              </span>
            ) : null}
          </div>
          <p className="text-xs text-text-faint">
            <span className="font-mono" title={run.run_id}>
              #{run.run_id}
            </span>
            {' | '}created{' '}
            <span title={formatDateTime(run.created_at)}>
              {formatRelative(run.created_at)}
            </span>
          </p>
        </div>
        <RunActions
          run={run}
          onDone={onChanged}
          only={apply === null ? 'all' : 'cancel'}
        />
      </div>

      <dl className="grid grid-cols-2 divide-x divide-line rounded-lg border border-line bg-panel sm:grid-cols-3">
        <Metric label="Plan and apply duration">
          {elapsed === null ? 'Not started' : formatDuration(elapsed)}
        </Metric>
        <PlanSummary run={run} />
        <Metric label="Mode">
          {run.plan_only ? 'Plan only' : 'Plan and apply'}
        </Metric>
      </dl>

      {run.error === undefined ||
      run.error === null ||
      run.error === '' ? null : (
        <ErrorNotice error={new Error(run.error)} />
      )}

      <Disclosure
        title="Run details"
        summary={
          <span title={formatDateTime(run.created_at)}>
            created {formatRelative(run.created_at)}
          </span>
        }
        open={isOpen('details')}
        onToggle={() => {
          toggle('details');
        }}
      >
        <RunProperties run={run} />
      </Disclosure>

      <Disclosure
        title={phaseTitle('Plan', plan)}
        tone={phaseTone(plan)}
        summary={planSummaryText(run)}
        open={isOpen('plan')}
        onToggle={() => {
          toggle('plan');
        }}
      >
        <PhaseBody
          run={run}
          phase="plan"
          status={plan}
          live={run.status === 'pending' || run.status === 'planning'}
        />
      </Disclosure>

      {apply === null ? null : (
        <Disclosure
          title={phaseTitle('Apply', apply)}
          tone={phaseTone(apply)}
          summary={apply === 'finished' ? applySummaryText(run) : undefined}
          open={isOpen('apply')}
          onToggle={() => {
            toggle('apply');
          }}
        >
          {apply === 'pending' ? (
            <ApplyPending run={run} onDone={onChanged} />
          ) : apply === 'discarded' ? (
            <p className="text-sm text-text-muted">
              The plan was discarded and nothing was applied.
            </p>
          ) : (
            <PhaseBody
              run={run}
              phase="apply"
              status={apply}
              live={run.status === 'applying'}
            />
          )}
        </Disclosure>
      )}
    </div>
  );
}

/** The title of a phase section, for example "Plan finished". */
function phaseTitle(phase: 'Plan' | 'Apply', status: PhaseStatus): string {
  const words: Record<PhaseStatus, string> = {
    queued: 'queued',
    running: 'running',
    finished: 'finished',
    pending: 'pending',
    errored: 'errored',
    cancelled: 'cancelled',
    discarded: 'discarded',
  };
  return `${phase} ${words[status]}`;
}

/** The dot colour beside a phase title. */
function phaseTone(status: PhaseStatus): RunTone {
  switch (status) {
    case 'running':
      return 'running';
    case 'finished':
      return 'success';
    case 'pending':
      return 'attention';
    case 'errored':
      return 'danger';
    default:
      return 'neutral';
  }
}

/** "Resources: 3 to add, 1 to change, 0 to destroy", once a plan reported. */
function planSummaryText(run: Run): string | undefined {
  if (run.changes === null || run.changes === undefined) {
    return undefined;
  }
  const { add, change, destroy } = run.changes;
  return `Resources: ${String(add)} to add, ${String(change)} to change, ${String(destroy)} to destroy`;
}

/** "Resources: 3 added, 1 changed, 0 destroyed", once an apply finished. */
function applySummaryText(run: Run): string | undefined {
  if (run.changes === null || run.changes === undefined) {
    return undefined;
  }
  const { add, change, destroy } = run.changes;
  return `Resources: ${String(add)} added, ${String(change)} changed, ${String(destroy)} destroyed`;
}

/** One figure in the metric row. */
function Metric({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <div className="min-w-0 px-4 py-3">
      <dt className="text-xs text-text-faint">{label}</dt>
      <dd className="mt-0.5 truncate text-sm text-text">{children}</dd>
    </div>
  );
}

/** The plan's resource counts as a metric, or the words for a plan that has not reported. */
function PlanSummary({ run }: { run: Run }): React.ReactElement {
  if (run.changes === null || run.changes === undefined) {
    return <Metric label="Resources changed">Not reported yet</Metric>;
  }
  const { add, change, destroy } = run.changes;
  return (
    <div
      data-testid="plan-summary"
      aria-label="Plan summary"
      className="min-w-0 px-4 py-3"
    >
      <dt className="text-xs text-text-faint">Resources changed</dt>
      <dd className="mt-0.5 flex items-center gap-3 font-mono text-sm tabular-nums">
        <span className="text-success" title="To add">
          +{add}
        </span>
        <span className="text-warning" title="To change">
          ~{change}
        </span>
        <span className="text-danger" title="To destroy">
          -{destroy}
        </span>
      </dd>
    </div>
  );
}

/** The timestamps and the log of one phase. */
function PhaseBody({
  run,
  phase,
  status,
  live,
}: {
  run: Run;
  phase: 'plan' | 'apply';
  status: PhaseStatus;
  live: boolean;
}): React.ReactElement {
  const finished =
    phase === 'plan' &&
    run.finished_at === null &&
    run.status !== 'planning' &&
    run.status !== 'pending'
      ? null
      : run.finished_at;
  return (
    <div className="space-y-3">
      <p className="text-xs text-text-faint">
        {run.started_at === null || run.started_at === undefined ? (
          status === 'queued' ? (
            'Waiting for the workspace to be free.'
          ) : (
            'Not started'
          )
        ) : (
          <>
            Started{' '}
            <span title={formatDateTime(run.started_at)}>
              {formatRelative(run.started_at)}
            </span>
            {finished === null || finished === undefined ? null : (
              <>
                {' | '}Finished{' '}
                <span title={formatDateTime(finished)}>
                  {formatRelative(finished)}
                </span>
              </>
            )}
          </>
        )}
      </p>
      {status === 'queued' ? null : (
        <RunLogViewer runId={run.run_id} phase={phase} live={live} />
      )}
    </div>
  );
}

/** The apply section while the plan waits on a person. */
function ApplyPending({
  run,
  onDone,
}: {
  run: Run;
  onDone: () => void;
}): React.ReactElement {
  return (
    <div className="space-y-3">
      <p className="text-sm text-text-muted">
        {run.status === 'awaiting_confirmation'
          ? 'The plan finished and needs confirmation before it is applied.'
          : 'The plan finished. Confirmation opens once the run is ready for it.'}
      </p>
      <RunActions run={run} onDone={onDone} only="decide" />
    </div>
  );
}

/** One row of the run details. */
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
    <div className="grid grid-cols-[8rem_minmax(0,1fr)] gap-2 py-1.5 text-xs">
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

/** The run's identifiers and timestamps. */
function RunProperties({ run }: { run: Run }): React.ReactElement {
  return (
    <dl className="divide-y divide-line">
      <Property label="Run id" mono>
        {run.run_id}
      </Property>
      <Property label="Workspace" mono>
        <Link
          to={`/workspaces/${run.workspace_id}`}
          className="text-accent hover:text-accent-hover hover:underline"
        >
          {run.workspace_id}
        </Link>
      </Property>
      <Property label="Configuration" mono>
        <Link
          to={`/workspaces/${run.workspace_id}/configuration-versions`}
          className="text-accent hover:text-accent-hover hover:underline"
        >
          {run.config_version_id}
        </Link>
      </Property>
      <Property label="Mode">
        {run.plan_only ? 'Plan only' : 'Plan and apply'}
      </Property>
      {run.queued_behind === undefined || run.queued_behind === null ? null : (
        <Property label="Queued behind" mono>
          {run.queued_behind}
        </Property>
      )}
      <When label="Created" iso={run.created_at} />
      <When label="Started" iso={run.started_at} />
      <When label="Finished" iso={run.finished_at} />
    </dl>
  );
}

/**
 * The cancel, confirm and discard buttons, each shown only in the states the
 * gating module allows.
 *
 * Cancel sits beside the title while a phase runs; confirm and discard sit in
 * the apply section, which is where the decision is read. Confirm is not step
 * up gated: `@webbpulse/auth` exposes `stepUp` only as a TOTP or recovery code
 * exchange, with no passkey ceremony, so a passkey step up would have to be
 * written here rather than reused. Reported as a gap.
 */
function RunActions({
  run,
  onDone,
  only,
}: {
  run: Run;
  onDone: () => void;
  only: 'cancel' | 'decide' | 'all';
}): React.ReactElement | null {
  const [busy, setBusy] = useState<'confirm' | 'cancel' | 'discard' | null>(
    null
  );
  const [error, setError] = useState<unknown>(null);

  const allowConfirm = only !== 'cancel' && canConfirm(run);
  const allowDiscard = only !== 'cancel' && canDiscard(run);
  const allowCancel = only !== 'decide' && canCancel(run);

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
    <div className="flex flex-col items-start gap-2">
      <div className="flex flex-wrap items-center gap-2">
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
            Confirm & apply
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
            Discard run
          </Button>
        ) : null}
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
      </div>
      <ErrorNotice error={error} />
    </div>
  );
}
