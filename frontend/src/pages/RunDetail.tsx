/** The run detail page: state, plan counts, the log tail and the three actions. */

import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { usePolledQuery } from '@webbpulse/api-client/react';
import { useAuthClient } from '@webbpulse/auth/react';

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
import { ErrorNotice, Spinner, StateBadge } from '../components';
import { RunLogViewer } from '../components/RunLogViewer';

/** The run detail page. */
export function RunDetail(): React.ReactElement {
  const { runId = '' } = useParams<{ runId: string }>();
  const auth = useAuthClient();
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
      setPhase(defaultPhase(run.state));
    }
  }, [run, phase]);

  if (query.isLoading) {
    return <Spinner label="Loading the run" />;
  }
  if (run === null) {
    return <ErrorNotice error={query.error ?? new Error('Run not found.')} />;
  }

  const shownPhase = phase ?? defaultPhase(run.state);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-mono text-lg text-surface-50">{run.run_id}</h1>
          <p className="text-sm text-surface-400">
            <Link
              to={`/workspaces/${run.workspace_id}`}
              className="text-brand-300 hover:text-brand-200"
            >
              {run.workspace_id}
            </Link>
          </p>
        </div>
        <StateBadge state={run.state} />
      </div>

      <ErrorNotice error={query.error} />
      <PlanSummary run={run} />
      <RunActions
        run={run}
        onDone={() => {
          void query.refetch();
        }}
      />

      <div className="space-y-2">
        <div role="tablist" className="flex gap-1">
          {(['plan', 'apply'] as const).map((candidate) => (
            <button
              key={candidate}
              type="button"
              role="tab"
              aria-selected={shownPhase === candidate}
              disabled={candidate === 'apply' && !hasApplyPhase(run.state)}
              onClick={() => {
                setPhase(candidate);
              }}
              className={`rounded-md px-3 py-1.5 text-sm disabled:opacity-40 ${
                shownPhase === candidate
                  ? 'bg-surface-800 text-surface-50'
                  : 'text-surface-300'
              }`}
            >
              {candidate === 'plan' ? 'Plan log' : 'Apply log'}
            </button>
          ))}
        </div>
        <RunLogViewer
          runId={run.run_id}
          phase={shownPhase}
          live={isActive(run.state)}
        />
      </div>
    </div>
  );
}

/** The plan's resource counts, or a sentence when the plan has not reported. */
function PlanSummary({ run }: { run: Run }): React.ReactElement {
  if (run.changes === null || run.changes === undefined) {
    return <p className="text-sm text-surface-400">No plan summary yet.</p>;
  }
  const { add, change, destroy } = run.changes;
  return (
    <dl
      data-testid="plan-summary"
      aria-label="Plan summary"
      className="flex gap-6 rounded-lg border border-surface-700 bg-surface-800 p-4 text-sm"
    >
      <div>
        <dt className="text-surface-400">To add</dt>
        <dd className="font-mono text-emerald-300">{add}</dd>
      </div>
      <div>
        <dt className="text-surface-400">To change</dt>
        <dd className="font-mono text-amber-300">{change}</dd>
      </div>
      <div>
        <dt className="text-surface-400">To destroy</dt>
        <dd className="font-mono text-rose-300">{destroy}</dd>
      </div>
    </dl>
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
  const [busy, setBusy] = useState(false);
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
    setBusy(true);
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
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        {busy ? <Spinner label="Working" /> : null}
        {allowConfirm ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              void act('confirm');
            }}
            className="rounded-md bg-emerald-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
          >
            Confirm and apply
          </button>
        ) : null}
        {allowDiscard ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              void act('discard');
            }}
            className="rounded-md border border-surface-600 px-3 py-2 text-sm text-surface-200 disabled:opacity-60"
          >
            Discard
          </button>
        ) : null}
        {allowCancel ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              void act('cancel');
            }}
            className="rounded-md border border-rose-600 px-3 py-2 text-sm text-rose-300 disabled:opacity-60"
          >
            Cancel run
          </button>
        ) : null}
      </div>
      <ErrorNotice error={error} />
    </div>
  );
}
