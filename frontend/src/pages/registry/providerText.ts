/** The addresses, snippets and keys the provider pages share. */

import type { Provider, ProviderVersion } from '../../api';

/** The refetch key of the provider list. */
export const PROVIDERS_KEY = 'registry-providers';

/** The refetch key of one provider and its versions. */
export function providerKey(
  namespace: string,
  type: string
): readonly string[] {
  return ['registry-provider', namespace, type];
}

/** The page of one provider. */
export function providerPagePath(
  provider: Pick<Provider, 'namespace' | 'type'>
): string {
  return `/registry/providers/${[provider.namespace, provider.type]
    .map(encodeURIComponent)
    .join('/')}`;
}

/** The `required_providers` block that installs a version from this host. */
export function providerSnippet(
  host: string,
  provider: Pick<Provider, 'namespace' | 'type'>,
  version: string | null
): string {
  const lines = [
    'terraform {',
    '  required_providers {',
    `    ${provider.type} = {`,
    `      source  = "${host}/${provider.namespace}/${provider.type}"`,
  ];
  if (version !== null) {
    lines.push(`      version = "${version}"`);
  }
  lines.push('    }', '  }', '}');
  return lines.join('\n');
}

/** The newest published version. */
export function latestPublishedProvider(
  versions: readonly ProviderVersion[]
): ProviderVersion | null {
  return versions.find((version) => version.status === 'published') ?? null;
}

/** The type a `terraform-provider-<type>` repository implies, as the API derives it. */
export function typeFromRepository(fullName: string): string | null {
  const repository = fullName.split('/').pop() ?? '';
  const match = /^terraform-provider-([0-9a-z][0-9a-z-]{0,62})$/.exec(
    repository
  );
  return match?.[1] ?? null;
}
