import { describe, expect, it } from 'vitest';

import { aResourceChange } from '../../test-helpers/fixtures';
import {
  actionGlyph,
  actionLabel,
  actionTone,
  attributeDiffs,
  formatPlanValue,
  planSummaryLine,
  unchangedCount,
} from './planDiff';
import type { PlanAction } from '../../api/runPlan';

describe('actionGlyph', () => {
  it('prints the glyph the engine prints for each action', () => {
    const glyphs: Record<PlanAction, string> = {
      create: '+',
      update: '~',
      delete: '-',
      replace: '-/+',
      read: '<=',
      'no-op': ' ',
    };
    for (const [action, glyph] of Object.entries(glyphs)) {
      expect(actionGlyph(action as PlanAction)).toBe(glyph);
    }
  });
});

describe('actionTone', () => {
  it('gives each action its own colour family', () => {
    expect(actionTone('create')).toBe('add');
    expect(actionTone('update')).toBe('change');
    expect(actionTone('delete')).toBe('destroy');
    expect(actionTone('replace')).toBe('replace');
    expect(actionTone('read')).toBe('read');
    expect(actionTone('no-op')).toBe('none');
  });
});

describe('actionLabel', () => {
  it('says what the action does in words', () => {
    expect(actionLabel('create')).toBe('created');
    expect(actionLabel('replace')).toBe('replaced');
    expect(actionLabel('no-op')).toBe('unchanged');
  });
});

describe('attributeDiffs', () => {
  it('reads every attribute of a create as added', () => {
    const diffs = attributeDiffs(aResourceChange());
    const bucket = diffs.find((diff) => diff.key === 'bucket');
    expect(bucket?.kind).toBe('added');
    expect(bucket?.after).toBe('platform-logs');
  });

  it('marks an attribute only known once applied', () => {
    const diffs = attributeDiffs(aResourceChange());
    const arn = diffs.find((diff) => diff.key === 'arn');
    expect(arn?.afterUnknown).toBe(true);
  });

  it('separates changed attributes from unchanged ones', () => {
    const diffs = attributeDiffs(
      aResourceChange({
        action: 'update',
        before: { name: 'runner', max_session_duration: 3600, path: '/' },
        after: { name: 'runner', max_session_duration: 7200, path: '/' },
        after_unknown: {},
      })
    );
    const duration = diffs.find((diff) => diff.key === 'max_session_duration');
    expect(duration?.kind).toBe('changed');
    expect(duration?.before).toBe(3600);
    expect(duration?.after).toBe(7200);
    expect(unchangedCount(diffs)).toBe(2);
  });

  it('reads every attribute of a delete as removed', () => {
    const diffs = attributeDiffs(
      aResourceChange({
        action: 'delete',
        before: { name: 'old-queue', delay_seconds: 0 },
        after: null,
        after_unknown: {},
      })
    );
    expect(diffs.every((diff) => diff.kind === 'removed')).toBe(true);
  });

  it('leaves out the null attributes of a create', () => {
    const diffs = attributeDiffs(
      aResourceChange({
        after: { length: 2, keepers: null, prefix: null },
        after_unknown: { id: true },
      })
    );
    expect(diffs.map((diff) => diff.key)).toEqual(['id', 'length']);
  });

  it('leaves out the null attributes of a delete', () => {
    const diffs = attributeDiffs(
      aResourceChange({
        action: 'delete',
        before: { id: 'lucky-horse', keepers: null },
        after: null,
        after_unknown: {},
      })
    );
    expect(diffs.map((diff) => diff.key)).toEqual(['id']);
  });

  it('keeps an attribute that goes to or from null in an update', () => {
    const diffs = attributeDiffs(
      aResourceChange({
        action: 'update',
        before: { description: 'old', tags: null, note: null },
        after: { description: null, tags: { team: 'platform' }, note: null },
        after_unknown: {},
      })
    );
    expect(diffs.map((diff) => [diff.key, diff.kind])).toEqual([
      ['description', 'changed'],
      ['tags', 'changed'],
    ]);
  });

  it('flags the attribute that forces a replacement', () => {
    const diffs = attributeDiffs(
      aResourceChange({
        action: 'replace',
        before: { identifier: 'primary', engine: 'postgres' },
        after: { identifier: 'primary-v2', engine: 'postgres' },
        after_unknown: {},
        replace_paths: [['identifier']],
      })
    );
    const identifier = diffs.find((diff) => diff.key === 'identifier');
    const engine = diffs.find((diff) => diff.key === 'engine');
    expect(identifier?.forcesReplacement).toBe(true);
    expect(engine?.forcesReplacement).toBe(false);
  });

  it('marks an attribute sensitive on either side', () => {
    const diffs = attributeDiffs(
      aResourceChange({
        action: 'update',
        before: { password: 'old', name: 'db' },
        after: { password: 'new', name: 'db' },
        after_unknown: {},
        before_sensitive: { password: true },
        after_sensitive: { password: true },
      })
    );
    const password = diffs.find((diff) => diff.key === 'password');
    expect(password?.sensitive).toBe(true);
  });

  it('lists an unknown only attribute of a read', () => {
    const diffs = attributeDiffs(
      aResourceChange({
        mode: 'data',
        action: 'read',
        before: null,
        after: null,
        after_unknown: { account_id: true },
      })
    );
    expect(diffs).toHaveLength(1);
    expect(diffs[0]?.key).toBe('account_id');
    expect(diffs[0]?.afterUnknown).toBe(true);
  });

  it('reads an unchanged resource as having nothing changed', () => {
    const diffs = attributeDiffs(
      aResourceChange({
        action: 'no-op',
        before: { description: 'state', enabled: true },
        after: { description: 'state', enabled: true },
        after_unknown: {},
      })
    );
    expect(unchangedCount(diffs)).toBe(2);
  });

  it('orders attributes by name', () => {
    const diffs = attributeDiffs(
      aResourceChange({
        action: 'create',
        before: null,
        after: { zeta: 1, alpha: 2, mid: 3 },
        after_unknown: {},
      })
    );
    expect(diffs.map((diff) => diff.key)).toEqual(['alpha', 'mid', 'zeta']);
  });
});

describe('formatPlanValue', () => {
  it('quotes strings and prints the rest plainly', () => {
    expect(formatPlanValue('a')).toBe('"a"');
    expect(formatPlanValue(3)).toBe('3');
    expect(formatPlanValue(true)).toBe('true');
    expect(formatPlanValue(null)).toBe('null');
    expect(formatPlanValue(undefined)).toBe('-');
    expect(formatPlanValue({ a: 1 })).toContain('"a": 1');
  });
});

describe('planSummaryLine', () => {
  it('states the counts the way a plan states them', () => {
    expect(planSummaryLine({ add: 2, change: 1, destroy: 0 })).toBe(
      '2 to add, 1 to change, 0 to destroy'
    );
  });
});
