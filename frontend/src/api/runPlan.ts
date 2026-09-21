/**
 * A run's plan as structured data, for the plan result view.
 *
 * Every shape here is an alias into the generated contract, so a backend change
 * to the plan projection lands as a type error rather than a runtime surprise.
 * The backend summarises and redacts server side: the raw `terraform show -json`
 * document never reaches the browser, and every value the plan marked sensitive
 * arrives already replaced by {@link SENSITIVE_VALUE}.
 */

import type { RequestOptions } from '@webbpulse/api-client';

import { api } from './client';
import type {
  PlanAction,
  PlanMode,
  PlanOutputChange,
  PlanResourceChange,
  RunChanges,
  RunPlan,
} from './types';

export type {
  PlanAction,
  PlanMode,
  PlanOutputChange,
  PlanResourceChange,
  RunPlan,
};

/** What a plan found: resources to add, change and destroy. */
export type PlanChanges = RunChanges;

/** What the backend substitutes for a value the plan marked sensitive. */
export const SENSITIVE_VALUE = '(sensitive value)';

/**
 * Reads one run's plan.
 *
 * Rejects with the client's `ApiError`. A 404 covers both a run that does not
 * exist and a run whose plan has not been uploaded yet, so a caller polling a
 * run that is still planning should treat it as not ready rather than as a
 * failure.
 */
export async function fetchRunPlan(
  runId: string,
  options: RequestOptions = {}
): Promise<RunPlan> {
  return api.getRunPlan(runId, options);
}

/** Whether the plan changes anything at all, which is what an empty state reads. */
export function planHasChanges(plan: RunPlan): boolean {
  return plan.has_changes === true;
}

/** The entries a plan actually changes, dropping the unchanged ones. */
export function changedResources(plan: RunPlan): PlanResourceChange[] {
  return (plan.resource_changes ?? []).filter(
    (entry) => entry.action !== 'no-op'
  );
}

/** The outputs a plan actually changes, dropping the unchanged ones. */
export function changedOutputs(plan: RunPlan): PlanOutputChange[] {
  return (plan.output_changes ?? []).filter(
    (entry) => entry.action !== 'no-op'
  );
}

/** The human label for each action, so a badge and a row agree on wording. */
const ACTION_LABELS: Record<PlanAction, string> = {
  create: 'Create',
  update: 'Update',
  delete: 'Destroy',
  replace: 'Replace',
  read: 'Read',
  'no-op': 'No changes',
};

/** The label an action renders as. */
export function planActionLabel(action: PlanAction): string {
  return ACTION_LABELS[action];
}

/** The sign Terraform prefixes a resource line with, per action. */
const ACTION_SYMBOLS: Record<PlanAction, string> = {
  create: '+',
  update: '~',
  delete: '-',
  replace: '-/+',
  read: '<=',
  'no-op': '',
};

/** The sign an action renders with, matching Terraform's own plan output. */
export function planActionSymbol(action: PlanAction): string {
  return ACTION_SYMBOLS[action];
}
