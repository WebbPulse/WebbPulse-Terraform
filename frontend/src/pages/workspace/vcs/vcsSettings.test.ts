import { describe, expect, it } from 'vitest';

import { aWorkspace } from '../../../test-helpers/fixtures';
import {
  EMPTY_VCS_SETTINGS,
  createBody,
  patternsOf,
  sameRepository,
  vcsSettingsOf,
  versionControlBody,
} from './vcsSettings';

const BASE = {
  name: 'platform',
  engine: 'terraform',
  engine_version: '1.11.0',
} as const;

describe('vcsSettings', () => {
  it('reads always trigger from file_triggers_enabled false', () => {
    expect(
      vcsSettingsOf(aWorkspace({ file_triggers_enabled: false })).triggerMode
    ).toBe('always');
    expect(vcsSettingsOf(aWorkspace()).triggerMode).toBe('paths');
  });

  it('drops blank pattern lines', () => {
    expect(patternsOf(' a/**\n\n  b/*.tf \n')).toEqual(['a/**', 'b/*.tf']);
  });

  it('compares repository names without case', () => {
    expect(sameRepository('WebbPulse/Infra', 'webbpulse/infra')).toBe(true);
    expect(sameRepository('WebbPulse/infra', null)).toBe(false);
  });

  it('leaves the branch out when it is empty', () => {
    const body = versionControlBody(aWorkspace(), 'WebbPulse/infra', {
      ...EMPTY_VCS_SETTINGS,
    });
    expect(body).not.toHaveProperty('tracked_branch');
    expect(body.vcs_repo).toBe('WebbPulse/infra');
  });

  it('creates CLI and API workspaces without repository fields', () => {
    expect(createBody(BASE, 'cli', null, EMPTY_VCS_SETTINGS)).toEqual(BASE);
    expect(
      createBody(BASE, 'api', 'WebbPulse/infra', EMPTY_VCS_SETTINGS)
    ).toEqual(BASE);
  });

  it('creates a version control workspace with its settings', () => {
    expect(
      createBody(BASE, 'vcs', 'WebbPulse/infra', {
        branch: ' staging ',
        workingDirectory: 'examples/first-run',
        triggerMode: 'paths',
        patterns: 'modules/**',
        speculativePlans: false,
      })
    ).toEqual({
      ...BASE,
      vcs_repo: 'WebbPulse/infra',
      tracked_branch: 'staging',
      working_directory: 'examples/first-run',
      file_triggers_enabled: true,
      trigger_patterns: ['modules/**'],
      speculative_plans: false,
    });
  });
});
