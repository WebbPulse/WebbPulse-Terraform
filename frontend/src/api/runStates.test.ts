import { describe, expect, it } from 'vitest';

import { aRun } from '../test-helpers/fixtures';
import {
  canCancel,
  canConfirm,
  canDiscard,
  defaultPhase,
  hasApplyPhase,
  isActive,
  isTerminal,
  runStateLabel,
  runTone,
} from './runStates';
import { RUN_STATES, type RunState } from './types';

describe('run state gating', () => {
  it('confirms only a run waiting on a person that is not plan only', () => {
    expect(canConfirm(aRun('awaiting_confirmation'))).toBe(true);
    expect(canConfirm(aRun('awaiting_confirmation', { plan_only: true }))).toBe(
      false
    );
    for (const state of RUN_STATES.filter(
      (candidate) => candidate !== 'awaiting_confirmation'
    )) {
      expect(canConfirm(aRun(state))).toBe(false);
    }
  });

  it('discards a run holding a finished plan, plan only included', () => {
    const discardable: RunState[] = ['planned', 'awaiting_confirmation'];
    for (const state of discardable) {
      expect(canDiscard(aRun(state))).toBe(true);
      expect(canDiscard(aRun(state, { plan_only: true }))).toBe(true);
    }
    for (const state of RUN_STATES.filter(
      (candidate) => !discardable.includes(candidate)
    )) {
      expect(canDiscard(aRun(state))).toBe(false);
    }
  });

  it('cancels only a phase that is running or queued', () => {
    const cancellable: RunState[] = ['pending', 'planning', 'applying'];
    for (const state of cancellable) {
      expect(canCancel(aRun(state))).toBe(true);
    }
    for (const state of RUN_STATES.filter(
      (candidate) => !cancellable.includes(candidate)
    )) {
      expect(canCancel(aRun(state))).toBe(false);
    }
  });

  it('never offers cancel and discard on the same state', () => {
    for (const state of RUN_STATES) {
      const run = aRun(state);
      expect(canCancel(run) && canDiscard(run)).toBe(false);
    }
  });

  it('never allows an action on a terminal run', () => {
    for (const state of RUN_STATES.filter(isTerminal)) {
      const run = aRun(state);
      expect(canConfirm(run)).toBe(false);
      expect(canCancel(run)).toBe(false);
      expect(canDiscard(run)).toBe(false);
    }
  });

  it('splits every state into exactly one of active and terminal, or neither', () => {
    for (const state of RUN_STATES) {
      expect(isActive(state) && isTerminal(state)).toBe(false);
    }
    expect(isActive('planning')).toBe(true);
    expect(isTerminal('applied')).toBe(true);
    expect(isActive('planned')).toBe(false);
    expect(isTerminal('planned')).toBe(false);
  });

  it('labels and tones every state', () => {
    for (const state of RUN_STATES) {
      expect(runStateLabel(state)).not.toBe('');
      expect(runTone(state)).toBeTruthy();
    }
    expect(runTone('errored')).toBe('danger');
    expect(runTone('awaiting_confirmation')).toBe('attention');
  });

  it('opens on the apply log once the run reached the apply phase', () => {
    expect(defaultPhase('applying')).toBe('apply');
    expect(defaultPhase('applied')).toBe('apply');
    expect(defaultPhase('planning')).toBe('plan');
    expect(defaultPhase('awaiting_confirmation')).toBe('plan');
    expect(hasApplyPhase('applying')).toBe(true);
    expect(hasApplyPhase('planned')).toBe(false);
  });
});
