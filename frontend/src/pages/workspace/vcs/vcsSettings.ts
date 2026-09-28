/** The VCS settings a form edits and the request fields they map to. */

import type { Workspace, WorkspaceCreate, WorkspaceUpdate } from '../../../api';

/** How pushes decide whether to start a run, in HCP Terraform's terms. */
export type TriggerMode = 'always' | 'paths';

/** The editable VCS settings, as the form holds them. */
export interface VcsSettings {
  branch: string;
  workingDirectory: string;
  triggerMode: TriggerMode;
  patterns: string;
  speculativePlans: boolean;
}

/**
 * The settings a workspace holds, as the form edits them.
 *
 * The branch starts empty, with the stored branch shown as its placeholder, so
 * typing never appends to a prefilled value and an empty field keeps the
 * branch the workspace already tracks.
 */
export function vcsSettingsOf(workspace: Workspace): VcsSettings {
  return {
    branch: '',
    workingDirectory: workspace.working_directory ?? '',
    triggerMode: workspace.file_triggers_enabled === false ? 'always' : 'paths',
    patterns: (workspace.trigger_patterns ?? []).join('\n'),
    speculativePlans: workspace.speculative_plans ?? true,
  };
}

/** The settings of a workspace that has none yet. */
export const EMPTY_VCS_SETTINGS: VcsSettings = {
  branch: '',
  workingDirectory: '',
  triggerMode: 'paths',
  patterns: '',
  speculativePlans: true,
};

/** The trigger patterns typed one per line, blanks dropped. */
export function patternsOf(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '');
}

/** The request fields the settings map to, leaving the branch to the caller. */
export function vcsFieldsBody(settings: VcsSettings): {
  working_directory: string;
  file_triggers_enabled: boolean;
  trigger_patterns: string[];
  speculative_plans: boolean;
} {
  return {
    working_directory: settings.workingDirectory.trim(),
    file_triggers_enabled: settings.triggerMode === 'paths',
    trigger_patterns:
      settings.triggerMode === 'paths' ? patternsOf(settings.patterns) : [],
    speculative_plans: settings.speculativePlans,
  };
}

/** Whether two repository names are the same, the way GitHub compares them. */
export function sameRepository(a: string | null, b: string | null): boolean {
  return a !== null && b !== null && a.toLowerCase() === b.toLowerCase();
}

/** The request that saves the form, sending the repository only when it changed. */
export function versionControlBody(
  workspace: Workspace,
  repository: string,
  settings: VcsSettings
): WorkspaceUpdate {
  const body: WorkspaceUpdate = vcsFieldsBody(settings);
  if (!sameRepository(repository, workspace.vcs_repo ?? null)) {
    body.vcs_repo = repository;
  }
  const branch = settings.branch.trim();
  if (branch !== '') {
    body.tracked_branch = branch;
  }
  return body;
}

/** How a workspace's runs start, the three ways HCP Terraform offers. */
export type Workflow = 'vcs' | 'cli' | 'api';

/** The workflow choices, in HCP Terraform's order. */
export const WORKFLOWS: readonly {
  id: Workflow;
  label: string;
  description: string;
}[] = [
  {
    id: 'vcs',
    label: 'Version control workflow',
    description:
      'Connect a GitHub repository. Pushes start runs and pull requests get plan-only runs.',
  },
  {
    id: 'cli',
    label: 'CLI-driven workflow',
    description:
      'Upload configuration from a terminal or a CI job and start runs from there.',
  },
  {
    id: 'api',
    label: 'API-driven workflow',
    description:
      'Upload configuration versions and start runs through the API, for custom tooling.',
  },
];

/** The create request for a workflow, adding the repository fields for version control. */
export function createBody(
  base: WorkspaceCreate,
  workflow: Workflow,
  repository: string | null,
  settings: VcsSettings
): WorkspaceCreate {
  if (workflow !== 'vcs' || repository === null) {
    return base;
  }
  const { working_directory, ...fields } = vcsFieldsBody(settings);
  const body: WorkspaceCreate = { ...base, ...fields, vcs_repo: repository };
  if (working_directory !== '') {
    body.working_directory = working_directory;
  }
  const branch = settings.branch.trim();
  if (branch !== '') {
    body.tracked_branch = branch;
  }
  return body;
}
