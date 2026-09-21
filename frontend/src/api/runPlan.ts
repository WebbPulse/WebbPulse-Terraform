/** The structured plan a run produced, as the run page renders it. */

import { API_BASE_URL, api } from './client';

/** What a plan does to one resource. */
export type PlanAction =
  'create' | 'update' | 'delete' | 'replace' | 'read' | 'no-op';

/** One resource the plan touches, with the attributes before and after. */
export interface PlanResourceChange {
  address: string;
  module_address: string;
  mode: 'managed' | 'data';
  type: string;
  name: string;
  provider_name: string;
  action: PlanAction;
  action_reason: string;
  before: object | null;
  after: object | null;
  after_unknown: object | null;
  replace_paths: (string | number)[][];
  before_sensitive: unknown;
  after_sensitive: unknown;
}

/** One output the plan changes. */
export interface PlanOutputChange {
  name: string;
  action: PlanAction;
  before: unknown;
  after: unknown;
  after_unknown: boolean;
  sensitive: boolean;
}

/** A run's plan, parsed from the engine's JSON plan. */
export interface RunPlan {
  run_id: string;
  terraform_version: string;
  changes: { add: number; change: number; destroy: number };
  resource_changes: PlanResourceChange[];
  output_changes: PlanOutputChange[];
  has_changes: boolean;
}

/**
 * Reads the structured plan a run produced.
 *
 * Goes through the shared {@link api} instance so the access token, the
 * refresh on a 401 and the error envelope are the ones every other read uses,
 * and rejects on a non-2xx the same way, which is what the run page's error
 * notice renders.
 */
export async function fetchRunPlan(
  runId: string,
  options: { signal?: AbortSignal } = {}
): Promise<RunPlan> {
  const response = await fetch(
    `${API_BASE_URL}/runs/${encodeURIComponent(runId)}/plan`,
    {
      credentials: 'include',
      headers: authHeaders(),
      ...(options.signal === undefined ? {} : { signal: options.signal }),
    }
  );
  if (!response.ok) {
    throw new Error(`The plan could not be read (${String(response.status)}).`);
  }
  return (await response.json()) as RunPlan;
}

/** The bearer header the shared auth client holds, or none while signed out. */
function authHeaders(): Record<string, string> {
  const token = api.getAuthClient().getAccessToken();
  return token === null ? {} : { Authorization: `Bearer ${token}` };
}
