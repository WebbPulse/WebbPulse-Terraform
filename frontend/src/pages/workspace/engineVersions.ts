/** The engine versions a workspace is offered, newest first. */

import type { Engine } from '../../api';

/** The engines a workspace can run. */
export const ENGINES: readonly Engine[] = ['terraform', 'tofu'];

/** How each engine is named in the interface. */
export const ENGINE_LABELS: Record<Engine, string> = {
  terraform: 'Terraform',
  tofu: 'OpenTofu',
};

/**
 * The newest patch of each recent minor line, newest first.
 *
 * The first entry of each list is the release baked into the runner image
 * (`runner/versions.env`), so a workspace on the default runs without a
 * download. Any other exact release still works: the runner installs it.
 */
export const ENGINE_VERSIONS: Record<Engine, readonly string[]> = {
  terraform: [
    '1.16.3',
    '1.15.9',
    '1.14.9',
    '1.13.5',
    '1.12.2',
    '1.11.4',
    '1.10.5',
    '1.9.8',
  ],
  tofu: ['1.12.6', '1.11.14', '1.10.10', '1.9.4', '1.8.11', '1.7.10'],
};

/** The version a new workspace on an engine starts on. */
export function defaultEngineVersion(engine: Engine): string {
  return ENGINE_VERSIONS[engine][0] ?? '';
}

/** Whether a version is one of the listed ones for an engine. */
export function isListedVersion(engine: Engine, version: string): boolean {
  return ENGINE_VERSIONS[engine].includes(version.trim());
}

/** Whether a typed version is an exact release, the only form the runner installs. */
export function isExactVersion(version: string): boolean {
  return /^v?\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$/.test(version.trim());
}

/** Why a typed version would be refused, or null when it is fine. */
export function engineVersionProblem(version: string): string | null {
  if (version.trim() === '') {
    return 'Choose an engine version.';
  }
  if (!isExactVersion(version)) {
    return 'The engine version has to be an exact release, such as 1.16.3.';
  }
  return null;
}
