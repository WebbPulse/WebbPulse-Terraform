import { describe, expect, it } from 'vitest';

import { aFreshWorkspace, aListedWorkspace } from '../test-helpers/fixtures';
import {
  browsableProjects,
  groupByProject,
  isEmptyDefault,
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

describe('isEmptyDefault', () => {
  it('holds only for the default project while it has no workspaces', () => {
    expect(
      isEmptyDefault({
        project_id: 'prj-default',
        is_default: true,
        workspace_count: 0,
      })
    ).toBe(true);
    expect(isEmptyDefault({ project_id: 'prj-default' })).toBe(true);
    expect(
      isEmptyDefault({
        project_id: 'prj-default',
        is_default: true,
        workspace_count: 1,
      })
    ).toBe(false);
    expect(
      isEmptyDefault({
        project_id: 'prj-01J000000000000000000001',
        is_default: false,
        workspace_count: 0,
      })
    ).toBe(false);
  });
});

describe('browsableProjects', () => {
  it('drops the empty default and keeps every other project in order', () => {
    const projects = [
      { project_id: 'prj-default', is_default: true, workspace_count: 0 },
      { project_id: 'prj-a', is_default: false, workspace_count: 0 },
      { project_id: 'prj-b', is_default: false, workspace_count: 3 },
    ];
    expect(browsableProjects(projects).map((p) => p.project_id)).toEqual([
      'prj-a',
      'prj-b',
    ]);
  });

  it('keeps the default once it holds a workspace', () => {
    const projects = [
      { project_id: 'prj-default', is_default: true, workspace_count: 1 },
    ];
    expect(browsableProjects(projects)).toEqual(projects);
  });
});
