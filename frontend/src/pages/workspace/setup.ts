/** The setup checklist's step logic, kept pure so it can be tested on its own. */

import {
  isActive,
  isConnected,
  type ConfigVersion,
  type Run,
  type RunState,
  type Workspace,
} from '../../api';

/** The three steps, in order. */
export type SetupStepId = 'connect' | 'upload' | 'plan';

/** Where a step stands: finished, the one to do now, or waiting on an earlier one. */
export type SetupStepStatus = 'done' | 'current' | 'blocked';

/** One step of the checklist. */
export interface SetupStep {
  id: SetupStepId;
  status: SetupStepStatus;
}

/** The step ids in the order they are shown and satisfied. */
export const SETUP_STEP_IDS: readonly SetupStepId[] = [
  'connect',
  'upload',
  'plan',
];

/** The states that mean a plan was produced, whatever happened after. */
const PLANNED_STATES: readonly RunState[] = [
  'planned',
  'awaiting_confirmation',
  'applying',
  'applied',
  'planned_and_finished',
  'discarded',
];

/** Whether any configuration version finished uploading. */
export function hasUploadedVersion(
  versions: readonly ConfigVersion[]
): boolean {
  return versions.some((version) => version.status === 'uploaded');
}

/** Whether any run got as far as a plan. */
export function hasPlannedRun(runs: readonly Run[]): boolean {
  return runs.some((run) => PLANNED_STATES.includes(run.status));
}

/**
 * Whether the workspace's account is connected, by the check or by a run.
 *
 * The check stamps the account id only when it passes, so a failed or missing
 * check leaves the step undone even after runs have planned through the role.
 * A run that produced a plan after the last check proves the runner assumed the
 * saved role, so it satisfies the step on its own.
 */
export function isAccountConnected(
  workspace: Workspace,
  runs: readonly Run[]
): boolean {
  if (isConnected(workspace)) {
    return true;
  }
  if ((workspace.run_role_arn ?? null) === null) {
    return false;
  }
  const checkedAt = workspace.run_role_checked_at ?? null;
  return hasPlannedRun(
    checkedAt === null ? runs : runs.filter((run) => run.created_at > checkedAt)
  );
}

/** The run still moving, if there is one, so the plan step can point at it. */
export function activeRun(runs: readonly Run[]): Run | null {
  return runs.find((run) => isActive(run.status)) ?? null;
}

/** The most recently created uploaded version, which a first plan runs on. */
export function latestUploadedVersion(
  versions: readonly ConfigVersion[]
): ConfigVersion | null {
  let latest: ConfigVersion | null = null;
  for (const version of versions) {
    if (version.status !== 'uploaded') {
      continue;
    }
    if (latest === null || version.created_at > latest.created_at) {
      latest = version;
    }
  }
  return latest;
}

/**
 * The checklist for a workspace.
 *
 * A step is done when satisfied. The first step not done is current, and every
 * later undone step is blocked, so the list always points at exactly one thing
 * to do next.
 */
export function setupSteps(
  workspace: Workspace,
  versions: readonly ConfigVersion[],
  runs: readonly Run[]
): SetupStep[] {
  const satisfied: Record<SetupStepId, boolean> = {
    connect: isAccountConnected(workspace, runs),
    upload: hasUploadedVersion(versions),
    plan: hasPlannedRun(runs),
  };
  let currentTaken = false;
  return SETUP_STEP_IDS.map((id) => {
    if (satisfied[id]) {
      return { id, status: 'done' };
    }
    if (!currentTaken) {
      currentTaken = true;
      return { id, status: 'current' };
    }
    return { id, status: 'blocked' };
  });
}

/** Whether every step is done, which is when the checklist goes away. */
export function isSetupComplete(steps: readonly SetupStep[]): boolean {
  return steps.every((step) => step.status === 'done');
}
