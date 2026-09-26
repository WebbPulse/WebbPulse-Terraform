import { describe, expect, it } from 'vitest';

import { missingForCreate, nameFromRepository } from './newWorkspaceRules';
import {
  defaultEngineVersion,
  engineVersionProblem,
  isListedVersion,
} from './workspace/engineVersions';

describe('nameFromRepository', () => {
  it('keeps a valid name and replaces what the API refuses', () => {
    expect(nameFromRepository('infra')).toBe('infra');
    expect(nameFromRepository('my repo+x')).toBe('my-repo-x');
    expect(nameFromRepository('.github')).toBe('github');
    expect(nameFromRepository('a'.repeat(120))).toHaveLength(90);
  });
});

describe('missingForCreate', () => {
  const ok = { name: 'platform', engine: { version: '1.16.3' } };

  it('asks for a repository first on the version control workflow', () => {
    expect(missingForCreate({ ...ok, workflow: 'vcs', repository: null })).toBe(
      'Choose a repository to connect.'
    );
  });

  it('asks for a name, then a valid one', () => {
    expect(
      missingForCreate({ ...ok, name: ' ', workflow: 'cli', repository: null })
    ).toBe('Enter a workspace name.');
    expect(
      missingForCreate({
        ...ok,
        name: '_x',
        workflow: 'cli',
        repository: null,
      })
    ).toMatch(/start with a letter or digit/);
  });

  it('checks the engine version last and passes a complete form', () => {
    expect(
      missingForCreate({
        ...ok,
        engine: { version: 'latest' },
        workflow: 'api',
        repository: null,
      })
    ).toBe('The engine version has to be an exact release, such as 1.16.3.');
    expect(
      missingForCreate({ ...ok, workflow: 'vcs', repository: 'o/r' })
    ).toBeNull();
  });
});

describe('engineVersions', () => {
  it('defaults each engine to the release the runner ships with', () => {
    expect(defaultEngineVersion('terraform')).toBe('1.16.3');
    expect(defaultEngineVersion('tofu')).toBe('1.12.6');
  });

  it('accepts exact releases, with or without a leading v', () => {
    expect(engineVersionProblem('1.16.4')).toBeNull();
    expect(engineVersionProblem('v1.9.0-beta1')).toBeNull();
    expect(engineVersionProblem('')).toBe('Choose an engine version.');
    expect(engineVersionProblem('~> 1.9')).not.toBeNull();
    expect(isListedVersion('terraform', '1.12.2')).toBe(true);
    expect(isListedVersion('tofu', '1.12.2')).toBe(false);
  });
});
