/** The pure rules behind the workspace lists: the sort choices, the address query, the name search and the project groups. */

import {
  DEFAULT_PROJECT_ID,
  type Project,
  type WorkspaceListItem,
  type WorkspaceSort,
} from '../api';

/** One choice of the sort control. */
export interface SortChoice {
  id: WorkspaceSort;
  label: string;
}

/** The orders the list offers, as HCP Terraform's sort menu names them. */
export const WORKSPACE_SORTS: readonly SortChoice[] = [
  { id: 'name', label: 'Name, A to Z' },
  { id: '-name', label: 'Name, Z to A' },
  { id: '-latest_run', label: 'Latest run' },
  { id: '-updated_at', label: 'Last updated' },
  { id: '-created_at', label: 'Newest created' },
  { id: 'status', label: 'Run status, needs attention first' },
];

/** The order a list starts in when the address names none. */
export const DEFAULT_WORKSPACE_SORT: WorkspaceSort = 'name';

/** The sort the address holds, or the default for a missing or unknown one. */
export function readSort(value: string | null): WorkspaceSort {
  const found = WORKSPACE_SORTS.find((choice) => choice.id === value);
  return found === undefined ? DEFAULT_WORKSPACE_SORT : found.id;
}

/**
 * Applies one change to the address's query, dropping a key whose value is
 * its default so a plain list keeps a plain address.
 */
export function withParam(
  current: URLSearchParams,
  key: string,
  value: string,
  fallback = ''
): URLSearchParams {
  const next = new URLSearchParams(current);
  if (value === fallback) {
    next.delete(key);
  } else {
    next.set(key, value);
  }
  return next;
}

/** The workspaces whose name holds the search, ignoring case. */
export function matchingName<T extends { name: string }>(
  items: readonly T[],
  search: string
): T[] {
  const needle = search.trim().toLowerCase();
  return needle === ''
    ? [...items]
    : items.filter((item) => item.name.toLowerCase().includes(needle));
}

/** One project and the workspaces in it, in the list's order. */
export interface ProjectGroup {
  project_id: string;
  name: string;
  workspaces: WorkspaceListItem[];
}

/**
 * Splits the sorted list into its projects, keeping the sort inside each one.
 * Projects come in the API's order, the default first; a workspace naming a
 * project the list has not caught up with yet still shows, under its id.
 */
export function groupByProject(
  workspaces: readonly WorkspaceListItem[],
  projects: readonly Pick<Project, 'project_id' | 'name'>[]
): ProjectGroup[] {
  const groups = new Map<string, ProjectGroup>(
    projects.map((project) => [
      project.project_id,
      { project_id: project.project_id, name: project.name, workspaces: [] },
    ])
  );
  for (const workspace of workspaces) {
    const projectId = workspace.project_id ?? DEFAULT_PROJECT_ID;
    let group = groups.get(projectId);
    if (group === undefined) {
      group = { project_id: projectId, name: projectId, workspaces: [] };
      groups.set(projectId, group);
    }
    group.workspaces.push(workspace);
  }
  return [...groups.values()];
}
