/** The addresses, snippets and labels the registry pages share. */

import type { Module, ModuleVersion, ModuleVersionStatus } from '../../api';

/** The refetch key of the module list. */
export const MODULES_KEY = 'registry-modules';

/** The refetch key of one module and its versions. */
export function moduleKey(
  namespace: string,
  name: string,
  provider: string
): readonly string[] {
  return ['registry-module', namespace, name, provider];
}

/** The page of one module, optionally at one version. */
export function modulePagePath(
  module: Pick<Module, 'namespace' | 'name' | 'provider'>,
  version?: string
): string {
  const path = `/registry/${[module.namespace, module.name, module.provider]
    .map(encodeURIComponent)
    .join('/')}`;
  return version === undefined
    ? path
    : `${path}?version=${encodeURIComponent(version)}`;
}

/** The host a module source names: the host serving this page, which serves the registry protocol. */
export function registryHost(): string {
  return window.location.host;
}

/** The full source address a module block names, with a `//modules/<name>` suffix for a submodule. */
export function sourceAddress(
  host: string,
  module: Pick<Module, 'namespace' | 'name' | 'provider'>,
  submodule?: string
): string {
  const base = `${host}/${module.namespace}/${module.name}/${module.provider}`;
  return submodule === undefined ? base : `${base}//modules/${submodule}`;
}

/** A module block's label, from a name that may hold characters HCL does not allow. */
export function blockLabel(name: string): string {
  const cleaned = name.replace(/[^A-Za-z0-9_-]/g, '_');
  return /^[A-Za-z_]/.test(cleaned) ? cleaned : `m_${cleaned}`;
}

/** The module block that uses a version, with each required input left to fill in. */
export function usageSnippet(
  source: string,
  label: string,
  version: string | null,
  required: readonly string[] = []
): string {
  const lines = [`module "${blockLabel(label)}" {`, `  source  = "${source}"`];
  if (version !== null) {
    lines.push(`  version = "${version}"`);
  }
  if (required.length > 0) {
    lines.push('');
    const width = Math.max(...required.map((name) => name.length));
    for (const name of required) {
      lines.push(`  ${name.padEnd(width)} = # required`);
    }
  }
  lines.push('}');
  return lines.join('\n');
}

/** The newest published version, the one a page opens on. */
export function latestPublished(
  versions: readonly ModuleVersion[]
): ModuleVersion | null {
  return versions.find((version) => version.status === 'published') ?? null;
}

/** Whether any version is still being published, so the page polls faster. */
export function hasPending(versions: readonly ModuleVersion[]): boolean {
  return versions.some((version) => version.status === 'pending');
}

/** The pill classes for each version status. */
export const STATUS_CLASSES: Record<ModuleVersionStatus, string> = {
  published: 'border-success-line bg-success-soft text-success',
  pending: 'border-running-line bg-running-soft text-running',
  failed: 'border-danger-line bg-danger-soft text-danger',
};

/** The label for each version status. */
export const STATUS_LABELS: Record<ModuleVersionStatus, string> = {
  published: 'Published',
  pending: 'Publishing',
  failed: 'Failed',
};

/** The environment variable Terraform reads a registry host's token from. */
export function tokenVariable(host: string): string {
  const hostname = host.replace(/:\d+$/, '');
  return `TF_TOKEN_${hostname.replace(/-/g, '__').replace(/\./g, '_')}`;
}

/** The name and provider a `terraform-<provider>-<name>` repository implies, as the API derives them. */
export function addressFromRepository(
  fullName: string
): { name: string; provider: string } | null {
  const repository = fullName.split('/').pop() ?? '';
  const match = /^terraform-([0-9a-z]+)-(.+)$/.exec(repository);
  return match?.[1] !== undefined && match[2] !== undefined
    ? { provider: match[1], name: match[2] }
    : null;
}
