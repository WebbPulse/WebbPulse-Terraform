/** The run page: its header, its progress, its plan and its raw logs. */

import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import {
  api,
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
  Dialog,
  ErrorNotice,
  Field,
  INPUT_CLASS,
  PageHeader,
  PlanSummary,
  PlanView,
  RelativeTime,
  RunLogs,
  RunSourceLine,
  RunTimeline,
  Spinner,
  StateBadge,
  Tabs,
  elapsedBetween,
  formatDuration,
  isDestroyRun,
  runKind,
  runTitle,
  shortRunId,
  useNow,
} from '../components';
import { useRunQuery } from './useRunQuery';
import { useOptionalWorkspace } from './workspaceContext';

/** The tabs the run's body switches between. */
type BodyTab = 'plan' | 'log';

/** The run page, inside a workspace or on its own. */
export function RunDetail(): React.ReactElement {
  const { runId = '' } = useParams<{ runId: string }>();
  const inWorkspace = useOptionalWorkspace() !== null;
  const query = useRunQuery(runId);
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
 * column with the raw log a tab away. Every action on the run lives in one
 * panel at the top of the main column, under the plan's counts and above its
 * resources, so a large plan cannot bury it: confirm and discard while the run
 * waits on a person, cancel while a phase runs, and nothing once it settles.
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
  const decision = <ConfirmationPanel run={run} onDone={onChanged} />;

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
            <PlanPanel run={run} planStatus={plan} decision={decision} />
          ) : (
            <>
              {decision}
              <RunLogs run={run} />
            </>
          )}
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
  const title = runTitle(run);
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
        <h2
          className="text-base font-semibold text-text-strong"
          title={run.vcs?.commit_message ?? undefined}
        >
          {title}
        </h2>
        <StateBadge state={run.status} />
        {isDestroyRun(run) ? <DestroyBadge /> : null}
        {run.plan_only ? (
          <span className="rounded-full border border-line-strong px-2 py-0.5 text-[11px] text-text-muted">
            Plan only
          </span>
        ) : null}
      </div>
      <p className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-text-faint">
        <span className="font-mono" title={run.run_id}>
          #{shortRunId(run.run_id)}
        </span>
        <span>
          {runKind(run)} triggered <RelativeTime iso={run.created_at} />
        </span>
        {run.vcs === undefined || run.vcs === null ? null : (
          <RunSourceLine run={run} />
        )}
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
          <RelativeTime iso={run.started_at} />
        ),
    },
    {
      label: 'Finished',
      value:
        run.finished_at === null || run.finished_at === undefined ? (
          <span className="text-text-faint">-</span>
        ) : (
          <RelativeTime iso={run.finished_at} />
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

/**
 * The plan, once the run has one to show.
 *
 * `decision` sits under the plan's counts and above its resources, or above
 * whatever stands in for the plan while it loads or cannot be read. It keeps
 * one place in the tree through all of those, so the plan arriving never
 * remounts it and a dialog it opened stays open.
 */
function PlanPanel({
  run,
  planStatus,
  decision,
}: {
  run: Run;
  planStatus: ReturnType<typeof planPhaseStatus>;
  decision: React.ReactNode;
}): React.ReactElement {
  const [plan, setPlan] = useState<RunPlan | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const ready = planStatus === 'finished';
  const applied = run.status === 'applied';
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
  }, [runId, ready, applied]);

  const shown = ready ? plan : null;
  let body: React.ReactNode;
  if (!ready) {
    body = (
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
  } else if (shown === null && loading) {
    body = (
      <div className="flex items-center gap-2 text-sm text-text-faint">
        <Spinner label="Loading the plan" className="size-4" />
        Loading the plan
      </div>
    );
  } else if (shown === null) {
    body = (
      <div className="space-y-2">
        <ErrorNotice error={error} />
        <p className="text-sm text-text-faint">
          The structured plan could not be read. The raw log has what the engine
          printed.
        </p>
      </div>
    );
  } else {
    body = (
      <PlanView
        plan={shown}
        applyChanges={run.apply_changes}
        isDestroy={run.is_destroy}
        showSummary={false}
      />
    );
  }

  return (
    <div className="space-y-5">
      {shown === null ? null : (
        <PlanSummary
          plan={shown}
          applyChanges={run.apply_changes}
          isDestroy={run.is_destroy}
        />
      )}
      {decision}
      {body}
    </div>
  );
}

/** A decision that asks for a comment before it is sent. */
type Decision = 'confirm' | 'discard';

/** How the run's action panel is tinted. */
type PanelTone = 'apply' | 'destroy' | 'running';

/** What the run's action panel says. */
interface PanelWords {
  tone: PanelTone;
  title: string;
  body: string;
}

/** The panel's tint, heading and explanation for the run's state. */
function panelWords(run: Run, allowConfirm: boolean): PanelWords {
  const destroy = isDestroyRun(run);
  if (run.status === 'awaiting_confirmation') {
    if (!allowConfirm) {
      return {
        tone: 'apply',
        title: 'This plan cannot be applied',
        body: 'This is a plan only run, so nothing is applied. Discard it once you have read the plan below.',
      };
    }
    return destroy
      ? {
          tone: 'destroy',
          title: 'This destroy plan needs confirmation',
          body: 'Review the plan below. Confirm to destroy every resource it lists in your AWS account, or discard it to keep them.',
        }
      : {
          tone: 'apply',
          title: 'This plan needs confirmation',
          body: 'Review the plan below. Confirm to apply it to your AWS account, or discard it to throw it away.',
        };
  }
  if (run.status === 'applying') {
    return {
      tone: 'running',
      title: destroy ? 'The destroy is running' : 'The apply is running',
      body: 'Cancelling stops the apply where it is. Anything already applied stays applied in your AWS account.',
    };
  }
  if (run.status === 'planning') {
    return {
      tone: 'running',
      title: 'The plan is running',
      body: 'Cancelling stops the plan before it finishes. Nothing is applied to your AWS account.',
    };
  }
  if (run.status === 'pending') {
    return {
      tone: 'running',
      title: 'This run is queued',
      body: 'It plans once the workspace is free. Cancel it to take it out of the queue.',
    };
  }
  return {
    tone: 'running',
    title: 'The plan finished',
    body: 'Confirmation opens once the run is ready for it. Discard the run to throw the plan away.',
  };
}

/** The classes one tone gives the panel and its icon. */
interface ToneClasses {
  panel: string;
  icon: string;
}

/** The panel's border and fill for each tone. */
const PANEL_TONE_CLASSES: Record<PanelTone, ToneClasses> = {
  apply: {
    panel: 'border-warning-line bg-warning-soft',
    icon: 'border-warning-line text-warning',
  },
  destroy: {
    panel: 'border-danger-line bg-danger-soft',
    icon: 'border-danger-line text-danger',
  },
  running: {
    panel: 'border-running-line bg-running-soft',
    icon: 'border-running-line text-running',
  },
};

/**
 * Every action on the run, in one panel at the top of the plan.
 *
 * While the run waits on confirmation it asks for confirm or discard, tinted
 * amber, or red for a destroy. Once confirmed, the same panel in the same
 * place offers cancel while the apply runs, as it does while the plan runs, and
 * it goes away when the run settles. The layout never changes between those,
 * with the buttons beside the explanation on a wide screen and full width
 * under it on a phone, so the swap does not move the page. Confirm and discard
 * open a dialog with an optional comment, as the hosted product does.
 */
function ConfirmationPanel({
  run,
  onDone,
}: {
  run: Run;
  onDone: () => void;
}): React.ReactElement | null {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [asking, setAsking] = useState<Decision | null>(null);
  const closeDialog = useCallback(() => {
    setAsking(null);
  }, []);

  const allowConfirm = canConfirm(run);
  const allowDiscard = canDiscard(run);
  const allowCancel = canCancel(run);

  if (!allowConfirm && !allowCancel && !allowDiscard) {
    return null;
  }

  const cancel = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await api.cancelRun(run.run_id);
      onDone();
    } catch (thrown) {
      setError(thrown);
    } finally {
      setBusy(false);
    }
  };

  const destroy = isDestroyRun(run);
  const words = panelWords(run, allowConfirm);
  const tone = PANEL_TONE_CLASSES[words.tone];
  const buttonWidth = 'flex-1 sm:flex-none';

  const dialog =
    asking === null ? null : (
      <DecisionDialog
        run={run}
        decision={asking}
        onClose={closeDialog}
        onDone={() => {
          setAsking(null);
          onDone();
        }}
      />
    );

  return (
    <section
      data-testid="confirmation-panel"
      data-placement="top"
      data-tone={words.tone}
      aria-labelledby="run-decision"
      className={`rounded-lg border shadow-sm ${tone.panel}`}
    >
      <div className="flex flex-col gap-4 p-4 sm:min-h-24 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <span
            aria-hidden="true"
            className={`mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border bg-panel ${tone.icon}`}
          >
            {words.tone === 'running' ? (
              <svg
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.75"
                strokeLinecap="round"
                className="size-3.5 motion-safe:animate-spin"
              >
                <path d="M8 2.5a5.5 5.5 0 1 1-5.5 5.5" />
              </svg>
            ) : (
              <svg
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.75"
                strokeLinecap="round"
                className="size-3.5"
              >
                <path d="M8 4.5v4.25" />
                <path d="M8 11.5h.01" />
              </svg>
            )}
          </span>
          <div className="min-w-0 space-y-1">
            <h3
              id="run-decision"
              className="text-sm font-semibold text-text-strong"
            >
              {words.title}
            </h3>
            <p className="max-w-prose text-sm text-text-muted">{words.body}</p>
          </div>
        </div>
        <div
          data-testid="run-actions"
          className="flex flex-wrap items-center gap-2 sm:shrink-0 sm:flex-nowrap"
        >
          {allowConfirm ? (
            <Button
              variant={destroy ? 'danger' : 'primary'}
              disabled={busy}
              className={buttonWidth}
              onClick={() => {
                setAsking('confirm');
              }}
            >
              {destroy ? 'Confirm & destroy' : 'Confirm & apply'}
            </Button>
          ) : null}
          {allowDiscard ? (
            <Button
              disabled={busy}
              className={buttonWidth}
              onClick={() => {
                setAsking('discard');
              }}
            >
              Discard run
            </Button>
          ) : null}
          {allowCancel ? (
            <Button
              variant="danger"
              disabled={busy}
              busy={busy}
              busyLabel="Working"
              className={buttonWidth}
              onClick={() => {
                void cancel();
              }}
            >
              Cancel run
            </Button>
          ) : null}
        </div>
      </div>
      {error === null ? null : (
        <div className="px-4 pb-4">
          <ErrorNotice error={error} />
        </div>
      )}
      {dialog}
    </section>
  );
}

/** The words each decision dialog uses. */
function decisionWords(
  decision: Decision,
  destroy: boolean
): { title: string; description: string; submit: string; busy: string } {
  if (decision === 'discard') {
    return {
      title: 'Discard run',
      description:
        'The plan is thrown away and nothing is applied. The workspace moves on to its next run.',
      submit: 'Discard run',
      busy: 'Discarding the run',
    };
  }
  return destroy
    ? {
        title: 'Confirm & destroy',
        description:
          'Every resource in the plan is destroyed in your AWS account. This cannot be undone.',
        submit: 'Confirm destroy',
        busy: 'Confirming the destroy',
      }
    : {
        title: 'Confirm & apply',
        description:
          'The plan is applied to your AWS account as it stands on this page.',
        submit: 'Confirm plan',
        busy: 'Confirming the plan',
      };
}

/** The confirm or discard dialog, with an optional comment kept on the run. */
function DecisionDialog({
  run,
  decision,
  onClose,
  onDone,
}: {
  run: Run;
  decision: Decision;
  onClose: () => void;
  onDone: () => void;
}): React.ReactElement {
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const destroy = isDestroyRun(run);
  const words = decisionWords(decision, destroy);
  const close = useCallback(() => {
    if (!busy) {
      onClose();
    }
  }, [busy, onClose]);

  const submit = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      if (decision === 'confirm') {
        await api.confirmRun(run.run_id, comment);
      } else {
        await api.discardRun(run.run_id, comment);
      }
      onDone();
    } catch (thrown) {
      setError(thrown);
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onClose={close}
      title={words.title}
      description={words.description}
    >
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <Field
          label="Comment"
          hint="Optional. Kept on the run beside who made the decision."
        >
          {(control) => (
            <textarea
              {...control}
              rows={3}
              maxLength={2000}
              value={comment}
              onChange={(event) => {
                setComment(event.target.value);
              }}
              className={`${INPUT_CLASS} h-auto py-1.5`}
            />
          )}
        </Field>
        <ErrorNotice error={error} />
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant={
              decision === 'confirm'
                ? destroy
                  ? 'danger'
                  : 'primary'
                : 'danger'
            }
            busy={busy}
            busyLabel={words.busy}
          >
            {words.submit}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
