/** The run page: its header, its progress, its plan and its raw logs. */

import { useEffect, useState } from 'react';
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
  type Run,
} from '../api';
import { fetchRunPlan, type RunPlan } from '../api/runPlan';
import {
  Button,
  DestroyBadge,
  ErrorNotice,
  PageHeader,
  PlanView,
  RunLogViewer,
  RunTimeline,
  Spinner,
  StateBadge,
  Tabs,
  elapsedBetween,
  formatDateTime,
  formatDuration,
  formatRelative,
  isDestroyRun,
  runKind,
  shortRunId,
  useNow,
} from '../components';
import { useOptionalWorkspace } from './workspaceContext';

/** The tabs the run's body switches between. */
type BodyTab = 'plan' | 'log';

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

/**
 * The run's header, its timeline and its plan.
 *
 * The layout follows the hosted product this replaces: what the run is at the
 * top, how far it has got down the left, and the plan itself in the main
 * column with the raw log a tab away.
 */
function RunBody({
  run,
  inWorkspace,
  onChanged,
}: {
  run: Run;
  inWorkspace: boolean;
  onChanged: () => void;
}): React.ReactElement {
  const [tab, setTab] = useState<BodyTab>('plan');
  const plan = planPhaseStatus(run);
  const apply = applyPhaseStatus(run);
  const logPhase = apply === null || apply === 'pending' ? 'plan' : 'apply';

  return (
    <div className="space-y-5">
      <RunHeader run={run} inWorkspace={inWorkspace} />

      {run.error === undefined ||
      run.error === null ||
      run.error === '' ? null : (
        <ErrorNotice error={new Error(run.error)} />
      )}

      <div className="grid gap-6 lg:grid-cols-[13rem_minmax(0,1fr)]">
        <aside
          aria-label="Run progress"
          className="lg:sticky lg:top-6 lg:self-start"
        >
          <RunTimeline run={run} />
          <RunFacts run={run} />
        </aside>

        <div className="min-w-0 space-y-4">
          <Tabs<BodyTab>
            label="Run views"
            value={tab}
            onChange={setTab}
            tabs={[
              { id: 'plan', label: 'Plan' },
              { id: 'log', label: 'Raw log' },
            ]}
          />
          {tab === 'plan' ? (
            <PlanPanel run={run} planStatus={plan} />
          ) : (
            <RunLogViewer
              runId={run.run_id}
              phase={logPhase}
              live={isActive(run.status)}
            />
          )}
          <ConfirmationPanel run={run} onDone={onChanged} />
        </div>
      </div>
    </div>
  );
}

/** The run's title, its state and who started it. */
function RunHeader({
  run,
  inWorkspace,
}: {
  run: Run;
  inWorkspace: boolean;
}): React.ReactElement {
  const title =
    run.message === undefined || run.message === ''
      ? `Run ${shortRunId(run.run_id)}`
      : run.message;
  return (
    <div className="space-y-2 border-b border-line pb-4">
      {inWorkspace ? (
        <nav aria-label="Run breadcrumb" className="text-xs text-text-faint">
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
        <h2 className="text-base font-semibold text-text-strong">{title}</h2>
        <StateBadge state={run.status} />
        {isDestroyRun(run) ? <DestroyBadge /> : null}
        {run.plan_only ? (
          <span className="rounded-full border border-line-strong px-2 py-0.5 text-[11px] text-text-muted">
            Plan only
          </span>
        ) : null}
      </div>
      <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-text-faint">
        <span className="font-mono" title={run.run_id}>
          #{shortRunId(run.run_id)}
        </span>
        <span aria-hidden="true">|</span>
        <span>
          {runKind(run)} triggered{' '}
          <span title={formatDateTime(run.created_at)}>
            {formatRelative(run.created_at)}
          </span>
        </span>
        <span aria-hidden="true">|</span>
        <Link
          to={`/workspaces/${run.workspace_id}/configuration-versions`}
          className="font-mono hover:text-accent hover:underline"
        >
          {run.config_version_id}
        </Link>
      </p>
    </div>
  );
}

/** The run's timings and identifiers, under the timeline. */
function RunFacts({ run }: { run: Run }): React.ReactElement {
  const now = useNow(isActive(run.status));
  const elapsed = elapsedBetween(run.started_at, run.finished_at, now);
  const rows: { label: string; value: React.ReactNode }[] = [
    {
      label: 'Duration',
      value: elapsed === null ? 'Not started' : formatDuration(elapsed),
    },
    {
      label: 'Started',
      value:
        run.started_at === null || run.started_at === undefined ? (
          <span className="text-text-faint">-</span>
        ) : (
          <span title={formatDateTime(run.started_at)}>
            {formatRelative(run.started_at)}
          </span>
        ),
    },
    {
      label: 'Finished',
      value:
        run.finished_at === null || run.finished_at === undefined ? (
          <span className="text-text-faint">-</span>
        ) : (
          <span title={formatDateTime(run.finished_at)}>
            {formatRelative(run.finished_at)}
          </span>
        ),
    },
  ];
  if (run.queued_behind !== null && run.queued_behind !== undefined) {
    rows.push({
      label: 'Queued behind',
      value: <span className="font-mono">{shortRunId(run.queued_behind)}</span>,
    });
  }
  return (
    <dl className="mt-4 space-y-1.5 border-t border-line pt-4 text-xs">
      {rows.map((row) => (
        <div
          key={row.label}
          className="flex items-baseline justify-between gap-2"
        >
          <dt className="text-text-faint">{row.label}</dt>
          <dd className="min-w-0 truncate text-right text-text">{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** The plan, once the run has one to show. */
function PlanPanel({
  run,
  planStatus,
}: {
  run: Run;
  planStatus: ReturnType<typeof planPhaseStatus>;
}): React.ReactElement {
  const [plan, setPlan] = useState<RunPlan | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const ready = planStatus === 'finished';
  const runId = run.run_id;

  useEffect(() => {
    if (!ready) {
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    fetchRunPlan(runId, { signal: controller.signal })
      .then((loaded) => {
        setPlan(loaded);
        setError(null);
      })
      .catch((thrown: unknown) => {
        if (!controller.signal.aborted) {
          setError(thrown);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => {
      controller.abort();
    };
  }, [runId, ready]);

  if (!ready) {
    return (
      <p
        data-testid="plan-pending"
        className="rounded-lg border border-dashed border-line px-4 py-8 text-center text-sm text-text-faint"
      >
        {planStatus === 'queued'
          ? 'The run is waiting for the workspace to be free.'
          : planStatus === 'running'
            ? 'The plan is running. Its resource changes appear here once it finishes.'
            : 'This run has no finished plan to show. The raw log has what the engine printed.'}
      </p>
    );
  }
  if (loading && plan === null) {
    return (
      <div className="flex items-center gap-2 text-sm text-text-faint">
        <Spinner label="Loading the plan" className="size-4" />
        Loading the plan
      </div>
    );
  }
  if (plan === null) {
    return (
      <div className="space-y-2">
        <ErrorNotice error={error} />
        <p className="text-sm text-text-faint">
          The structured plan could not be read. The raw log has what the engine
          printed.
        </p>
      </div>
    );
  }
  return <PlanView plan={plan} />;
}

/**
 * Confirm and apply, or discard, below the plan.
 *
 * Cancel lives here too while a phase is running, so every decision about the
 * run is in one place, under what it is a decision about.
 */
function ConfirmationPanel({
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
  const allowDiscard = canDiscard(run);
  const allowCancel = canCancel(run);

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

  const waiting = run.status === 'awaiting_confirmation';

  return (
    <section
      data-testid="confirmation-panel"
      aria-labelledby="run-decision"
      className={`space-y-3 rounded-lg border p-4 ${
        waiting ? 'border-warning-line bg-warning-soft' : 'border-line bg-panel'
      }`}
    >
      <h3 id="run-decision" className="text-sm font-semibold text-text-strong">
        {waiting
          ? 'This plan needs confirmation'
          : allowCancel
            ? 'Stop this run'
            : 'Decide on this plan'}
      </h3>
      <p className="max-w-prose text-sm text-text-muted">
        {waiting
          ? isDestroyRun(run)
            ? 'Confirm to destroy every resource listed above in your AWS account, or discard the plan to keep them.'
            : 'Confirm to apply the plan above to your AWS account, or discard it to throw it away.'
          : allowCancel
            ? 'Cancelling stops the phase that is running. Anything already applied stays applied.'
            : 'The plan finished. Confirmation opens once the run is ready for it.'}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        {allowConfirm ? (
          <Button
            variant={isDestroyRun(run) ? 'danger' : 'primary'}
            disabled={busy !== null}
            busy={busy === 'confirm'}
            busyLabel="Working"
            onClick={() => {
              void act('confirm');
            }}
          >
            {isDestroyRun(run) ? 'Confirm & destroy' : 'Confirm & apply'}
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
    </section>
  );
}
