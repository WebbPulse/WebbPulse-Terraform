/** The rules the new workspace page checks before it lets a workspace be created. */

import { engineVersionProblem } from './workspace/engineVersions';
import type { Workflow } from './workspace/vcs/vcsSettings';

/** The pattern a workspace name has to match, the same one the API enforces. */
export const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

/** The longest name the API accepts. */
export const NAME_MAX_LENGTH = 90;

/** A workspace name derived from a repository name, or empty when none fits. */
export function nameFromRepository(name: string): string {
  return name
    .replace(/[^A-Za-z0-9._-]/g, '-')
    .replace(/^[^A-Za-z0-9]+/, '')
    .slice(0, NAME_MAX_LENGTH);
}

/** What still stops the workspace being created, or null when nothing does. */
export function missingForCreate({
  name,
  engine,
  workflow,
  repository,
}: {
  name: string;
  engine: { version: string };
  workflow: Workflow;
  repository: string | null;
}): string | null {
  const trimmed = name.trim();
  if (workflow === 'vcs' && repository === null) {
    return 'Choose a repository to connect.';
  }
  if (trimmed === '') {
    return 'Enter a workspace name.';
  }
  if (!NAME_PATTERN.test(trimmed)) {
    return 'The name can use letters, digits, dots, underscores and hyphens, and has to start with a letter or digit.';
  }
  if (trimmed.length > NAME_MAX_LENGTH) {
    return `The name can be at most ${String(NAME_MAX_LENGTH)} characters.`;
  }
  return engineVersionProblem(engine.version);
}
