import { describe, expect, it } from 'vitest';

import {
  aConfigVersion,
  aFreshWorkspace,
  aRun,
  aWorkspace,
} from '../../test-helpers/fixtures';
import {
  activeRun,
  isSetupComplete,
  latestUploadedVersion,
  setupSteps,
} from './setup';

describe('setupSteps', () => {
  it('starts with connect current and the rest blocked', () => {
    expect(setupSteps(aFreshWorkspace(), [], [])).toEqual([
      { id: 'connect', status: 'current' },
      { id: 'upload', status: 'blocked' },
      { id: 'plan', status: 'blocked' },
    ]);
  });

  it('moves to upload once the account is connected', () => {
    expect(setupSteps(aWorkspace(), [], [])).toEqual([
      { id: 'connect', status: 'done' },
      { id: 'upload', status: 'current' },
      { id: 'plan', status: 'blocked' },
    ]);
  });

  it('needs a finished upload, not a pending one', () => {
    expect(
      setupSteps(aWorkspace(), [aConfigVersion({ status: 'pending' })], [])[1]
    ).toEqual({ id: 'upload', status: 'current' });
    expect(setupSteps(aWorkspace(), [aConfigVersion()], [])).toEqual([
      { id: 'connect', status: 'done' },
      { id: 'upload', status: 'done' },
      { id: 'plan', status: 'current' },
    ]);
  });

  it('counts a plan that ran, whatever happened after it', () => {
    for (const state of ['planned', 'applied', 'discarded'] as const) {
      const steps = setupSteps(aWorkspace(), [aConfigVersion()], [aRun(state)]);
      expect(isSetupComplete(steps)).toBe(true);
    }
    for (const state of ['pending', 'errored', 'cancelled'] as const) {
      const steps = setupSteps(aWorkspace(), [aConfigVersion()], [aRun(state)]);
      expect(steps[2]).toEqual({ id: 'plan', status: 'current' });
    }
  });

  it('marks a later step done even while an earlier one is current', () => {
    expect(setupSteps(aFreshWorkspace(), [aConfigVersion()], [])).toEqual([
      { id: 'connect', status: 'current' },
      { id: 'upload', status: 'done' },
      { id: 'plan', status: 'blocked' },
    ]);
  });
});

describe('latestUploadedVersion', () => {
  it('picks the newest uploaded version and skips pending ones', () => {
    const older = aConfigVersion({
      config_version_id: 'cv-1',
      created_at: '2026-09-17T00:00:00Z',
    });
    const newer = aConfigVersion({
      config_version_id: 'cv-2',
      created_at: '2026-09-18T00:00:00Z',
    });
    const pending = aConfigVersion({
      config_version_id: 'cv-3',
      status: 'pending',
      created_at: '2026-09-19T00:00:00Z',
    });
    expect(latestUploadedVersion([older, pending, newer])).toBe(newer);
    expect(latestUploadedVersion([pending])).toBeNull();
  });
});

describe('activeRun', () => {
  it('finds a run still moving', () => {
    expect(activeRun([aRun('applied'), aRun('planning')])?.status).toBe(
      'planning'
    );
    expect(activeRun([aRun('applied')])).toBeNull();
  });
});
