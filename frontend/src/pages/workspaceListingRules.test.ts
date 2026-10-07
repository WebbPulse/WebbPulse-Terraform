import { describe, expect, it } from 'vitest';

import { aFreshWorkspace, aListedWorkspace } from '../test-helpers/fixtures';
import {
  groupByProject,
  matchingName,
  readSort,
  withParam,
} from './workspaceListingRules';

describe('readSort', () => {
  it('keeps a known sort and falls back to name for anything else', () => {
    expect(readSort('-latest_run')).toBe('-latest_run');
    expect(readSort('colour')).toBe('name');
    expect(readSort(null)).toBe('name');
  });
});

describe('withParam', () => {
  it('sets a value and drops it again at its default', () => {
    const set = withParam(
      new URLSearchParams('q=cmp'),
      'sort',
      'status',
      'name'
    );
    expect(set.toString()).toBe('q=cmp&sort=status');
    expect(withParam(set, 'sort', 'name', 'name').toString()).toBe('q=cmp');
  });
});

describe('matchingName', () => {
  it('matches part of a name ignoring case', () => {
    const items = [{ name: 'cmp-prod' }, { name: 'Portfolio' }];
    expect(matchingName(items, 'PORT')).toEqual([{ name: 'Portfolio' }]);
    expect(matchingName(items, '  ')).toEqual(items);
  });
});

describe('groupByProject', () => {
  it('keeps the projects in order and the list order inside each one', () => {
    const inProject = 'prj-01J000000000000000000001';
    const groups = groupByProject(
      [
        aListedWorkspace(
          aFreshWorkspace({
            workspace_id: 'ws-b',
            name: 'b',
            project_id: inProject,
          })
        ),
        aListedWorkspace(aFreshWorkspace({ workspace_id: 'ws-a', name: 'a' })),
        aListedWorkspace(
          aFreshWorkspace({
            workspace_id: 'ws-c',
            name: 'c',
            project_id: inProject,
          })
        ),
        aListedWorkspace(
          aFreshWorkspace({
            workspace_id: 'ws-d',
            name: 'd',
            project_id: 'prj-01J000000000000000000009',
          })
        ),
      ],
      [
        { project_id: 'prj-default', name: 'Default Project' },
        { project_id: inProject, name: 'CarModPicker' },
      ]
    );
    expect(
      groups.map((group) => [
        group.name,
        group.workspaces.map((workspace) => workspace.name),
      ])
    ).toEqual([
      ['Default Project', ['a']],
      ['CarModPicker', ['b', 'c']],
      ['prj-01J000000000000000000009', ['d']],
    ]);
  });
});
