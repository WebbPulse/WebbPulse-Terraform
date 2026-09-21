import { describe, expect, it } from 'vitest';

import { mockFetch } from '../test-helpers/mockFetch';
import { TerraformApi } from './client';
import {
  SENSITIVE_VALUE,
  changedOutputs,
  changedResources,
  planActionLabel,
  planActionSymbol,
  planHasChanges,
} from './runPlan';
import type { PlanAction, RunPlan } from './types';

const BASE = 'https://api.staging.terraform.webbpulse.com/api/v1';

/** Every action the contract allows, so a map cannot silently miss one. */
const ACTIONS: PlanAction[] = [
  'create',
  'update',
  'delete',
  'replace',
  'read',
  'no-op',
];

/** A plan carrying `overrides` on top of an empty one. */
function aPlan(overrides: Partial<RunPlan> = {}): RunPlan {
  return {
    run_id: 'run-1',
    terraform_version: '1.13.3',
    changes: { add: 0, change: 0, destroy: 0 },
    resource_changes: [],
    output_changes: [],
    has_changes: false,
    ...overrides,
  };
}

describe('fetching a run plan', () => {
  it('reads the plan route for the run', async () => {
    const transport = mockFetch({
      'GET /api/v1/runs/run-1/plan': {
        body: aPlan({
          changes: { add: 2, change: 1, destroy: 1 },
          has_changes: true,
        }),
      },
    });
    const api = new TerraformApi({
      baseUrl: BASE,
      fetch: transport.fetch,
      retries: 0,
    });

    const plan = await api.getRunPlan('run-1');
    expect(plan.changes).toEqual({ add: 2, change: 1, destroy: 1 });
    expect(plan.terraform_version).toBe('1.13.3');
    expect(transport.requests[0]?.method).toBe('GET');
  });

  it('encodes the run id into the path', async () => {
    const transport = mockFetch({
      'GET /api/v1/runs/run%2F1/plan': { body: aPlan() },
    });
    const api = new TerraformApi({
      baseUrl: BASE,
      fetch: transport.fetch,
      retries: 0,
    });

    await api.getRunPlan('run/1');
    expect(transport.requests[0]?.url).toContain('/runs/run%2F1/plan');
  });

  it('rejects when the plan is not there yet', async () => {
    const transport = mockFetch({
      'GET /api/v1/runs/run-1/plan': {
        status: 404,
        body: { message: 'That run has no plan yet.' },
      },
    });
    const api = new TerraformApi({
      baseUrl: BASE,
      fetch: transport.fetch,
      retries: 0,
    });

    await expect(api.getRunPlan('run-1')).rejects.toThrow();
  });
});

describe('reading a plan', () => {
  it('reports whether the plan changes anything', () => {
    expect(planHasChanges(aPlan())).toBe(false);
    expect(planHasChanges(aPlan({ has_changes: true }))).toBe(true);
  });

  it('drops the unchanged resources and outputs', () => {
    const plan = aPlan({
      resource_changes: [
        {
          address: 'aws_s3_bucket.a',
          mode: 'managed',
          type: 'aws_s3_bucket',
          name: 'a',
          action: 'create',
        },
        {
          address: 'aws_s3_bucket.b',
          mode: 'managed',
          type: 'aws_s3_bucket',
          name: 'b',
          action: 'no-op',
        },
      ],
      output_changes: [
        { name: 'url', action: 'update' },
        { name: 'stable', action: 'no-op' },
      ],
    });

    expect(changedResources(plan).map((entry) => entry.address)).toEqual([
      'aws_s3_bucket.a',
    ]);
    expect(changedOutputs(plan).map((entry) => entry.name)).toEqual(['url']);
  });

  it('treats an absent list as empty', () => {
    const plan = aPlan();
    delete plan.resource_changes;
    delete plan.output_changes;
    expect(changedResources(plan)).toEqual([]);
    expect(changedOutputs(plan)).toEqual([]);
  });

  it('labels every action', () => {
    for (const action of ACTIONS) {
      expect(planActionLabel(action)).not.toBe('');
    }
    expect(planActionLabel('delete')).toBe('Destroy');
    expect(planActionLabel('replace')).toBe('Replace');
  });

  it('signs every changing action the way Terraform does', () => {
    expect(planActionSymbol('create')).toBe('+');
    expect(planActionSymbol('update')).toBe('~');
    expect(planActionSymbol('delete')).toBe('-');
    expect(planActionSymbol('replace')).toBe('-/+');
    expect(planActionSymbol('no-op')).toBe('');
  });

  it('names the string the backend redacts a sensitive value to', () => {
    expect(SENSITIVE_VALUE).toBe('(sensitive value)');
  });
});
