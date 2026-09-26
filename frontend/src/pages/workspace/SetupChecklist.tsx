/** The three step checklist a workspace leads with until its first plan. */

import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { invalidateQueries } from '@webbpulse/api-client/react';

import {
  RUN_ROLE_MISSING_MESSAGE,
  api,
  isRunRoleMissing,
  type ConfigVersion,
  type Run,
  type Workspace,
} from '../../api';
import { Button, ErrorNotice, runPath } from '../../components';
import { ConnectAccountPanel } from './ConnectAccountPanel';
import { UploadConfigForm } from './UploadConfigForm';
import {
  activeRun,
  latestUploadedVersion,
  type SetupStep,
  type SetupStepId,
} from './setup';

/** Props for {@link SetupChecklist}. */
export interface SetupChecklistProps {
  workspace: Workspace;
  steps: readonly SetupStep[];
  versions: readonly ConfigVersion[];
  runs: readonly Run[];
  /** The refetch keys for the workspace, its versions and its runs. */
  keys: { workspace: string; versions: string; runs: string };
}

/** What each step is called and what it asks for. */
const STEP_COPY: Record<SetupStepId, { title: string; summary: string }> = {
  connect: {
    title: 'Connect an AWS account',
    summary: 'Create a role the runner can assume and save its ARN.',
  },
  upload: {
    title: 'Upload a configuration',
    summary: 'A tar.gz of the directory holding your root module.',
  },
  plan: {
    title: 'Run a plan',
    summary: 'A plan only run, to see the runner work end to end.',
  },
};

/**
 * The checklist. Render it only while some step is not done.
 *
 * The current step opens on its own, and any step can be opened or closed by
 * hand whatever its status, the way the hosted product lets a step be read
 * ahead of the one before it.
 */
export function SetupChecklist({
  workspace,
  steps,
  versions,
  runs,
  keys,
}: SetupChecklistProps): React.ReactElement {
  const [expanded, setExpanded] = useState<
    Partial<Record<SetupStepId, boolean>>
  >({});
  const doneCount = steps.filter((step) => step.status === 'done').length;
  return (
    <section
      data-testid="setup-checklist"
      aria-labelledby="setup-checklist-title"
      className="rounded-lg border border-line bg-panel"
    >
      <div className="flex items-baseline justify-between gap-4 border-b border-line px-4 py-3">
        <div>
          <h2
            id="setup-checklist-title"
            className="text-sm font-semibold text-text-strong"
          >
            Set up this workspace
          </h2>
          <p className="text-xs text-text-faint">
            Three steps before the first run.
          </p>
        </div>
        <span className="font-mono text-xs text-text-faint">
          {doneCount} of {steps.length} done
        </span>
      </div>
      <ol className="divide-y divide-line">
        {steps.map((step, index) => {
          const open = expanded[step.id] ?? step.status === 'current';
          const bodyId = `setup-step-${step.id}-body`;
          return (
            <li
              key={step.id}
              data-testid={`setup-step-${step.id}`}
              data-status={step.status}
              className="px-4 py-3"
            >
              <div className="flex items-start gap-3">
                <StepMarker index={index} status={step.status} />
                <div className="min-w-0 flex-1">
                  <button
                    type="button"
                    aria-expanded={open}
                    aria-controls={bodyId}
                    onClick={() => {
                      setExpanded((current) => ({
                        ...current,
                        [step.id]: !open,
                      }));
                    }}
                    className="flex w-full items-start justify-between gap-3 rounded-md text-left focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none"
                  >
                    <span className="min-w-0">
                      <span
                        className={`block text-sm font-medium ${
                          step.status === 'blocked'
                            ? 'text-text-muted'
                            : 'text-text-strong'
                        }`}
                      >
                        {STEP_COPY[step.id].title}
                      </span>
                      <span className="block text-xs text-text-faint">
                        {step.status === 'done'
                          ? 'Done'
                          : STEP_COPY[step.id].summary}
                      </span>
                    </span>
                    <svg
                      viewBox="0 0 16 16"
                      className={`mt-0.5 size-4 shrink-0 text-text-faint transition-transform ${
                        open ? 'rotate-180' : ''
                      }`}
                      fill="none"
                      aria-hidden="true"
                    >
                      <path
                        d="m4 6 4 4 4-4"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  </button>
                  {open ? (
                    <div id={bodyId} className="mt-4">
                      <StepBody
                        id={step.id}
                        workspace={workspace}
                        versions={versions}
                        runs={runs}
                        keys={keys}
                      />
                    </div>
                  ) : null}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** The numbered or ticked circle at the left of a step. */
function StepMarker({
  index,
  status,
}: {
  index: number;
  status: SetupStep['status'];
}): React.ReactElement {
  const classes = {
    done: 'border-success-line bg-success-soft text-success',
    current: 'border-accent bg-accent-soft text-accent',
    blocked: 'border-line-strong bg-panel text-text-faint',
  }[status];
  return (
    <span
      aria-hidden="true"
      className={`mt-0.5 inline-flex size-5 shrink-0 items-center justify-center rounded-full border text-[11px] font-semibold ${classes}`}
    >
      {status === 'done' ? (
        <svg viewBox="0 0 12 12" className="size-3" fill="none">
          <path
            d="M2.5 6.5 5 9l4.5-6"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      ) : (
        index + 1
      )}
    </span>
  );
}

/** The interactive part of an open step. */
function StepBody({
  id,
  workspace,
  versions,
  runs,
  keys,
}: {
  id: SetupStepId;
  workspace: Workspace;
  versions: readonly ConfigVersion[];
  runs: readonly Run[];
  keys: SetupChecklistProps['keys'];
}): React.ReactElement {
  switch (id) {
    case 'connect':
      return (
        <ConnectAccountPanel workspace={workspace} queryKey={keys.workspace} />
      );
    case 'upload':
      return (
        <UploadConfigForm
          workspaceId={workspace.workspace_id}
          queryKey={keys.versions}
          label="Upload a configuration"
          fileLabel="Configuration archive (.tar.gz)"
          hint="Pack the directory holding your root module, for example: tar -czf config.tar.gz -C ./terraform ."
        />
      );
    case 'plan':
      return (
        <FirstPlan
          workspace={workspace}
          version={latestUploadedVersion(versions)}
          running={activeRun(runs)}
          runsKey={keys.runs}
        />
      );
  }
}

/** The button that starts the first plan only run, or the link to the one running. */
function FirstPlan({
  workspace,
  version,
  running,
  runsKey,
}: {
  workspace: Workspace;
  version: ConfigVersion | null;
  running: Run | null;
  runsKey: string;
}): React.ReactElement {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  if (running !== null) {
    return (
      <p className="text-sm text-text-muted">
        A run is in progress.{' '}
        <Link
          to={runPath(running)}
          className="text-accent hover:text-accent-hover hover:underline"
        >
          Follow it
        </Link>
        .
      </p>
    );
  }

  const start = async (): Promise<void> => {
    if (version === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const run = await api.createRun({
        workspace_id: workspace.workspace_id,
        config_version_id: version.config_version_id,
        plan_only: true,
      });
      invalidateQueries(runsKey);
      void navigate(
        runPath({ run_id: run.run_id, workspace_id: workspace.workspace_id })
      );
    } catch (thrown) {
      setError(
        isRunRoleMissing(thrown) ? new Error(RUN_ROLE_MISSING_MESSAGE) : thrown
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <p className="text-sm text-text-muted">
        Plans the latest configuration
        {version === null ? null : (
          <>
            {' '}
            <code className="font-mono text-xs text-text">
              {version.config_version_id}
            </code>
          </>
        )}{' '}
        without applying anything.
      </p>
      <Button
        variant="primary"
        busy={busy}
        busyLabel="Starting the plan"
        disabled={version === null}
        onClick={() => {
          void start();
        }}
      >
        Run a plan
      </Button>
      <ErrorNotice error={error} />
    </div>
  );
}
