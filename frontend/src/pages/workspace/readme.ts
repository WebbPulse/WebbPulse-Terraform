/** Which configuration version's README the overview shows, and where its links go. */

import type { ConfigVersion, ConfigVersionVcs } from '../../api';
import type { UrlResolver } from '../../components';

/**
 * The newest uploaded configuration version that is not a pull request's.
 *
 * A pull request's version runs plan only and may never merge, so like HCP
 * Terraform the overview describes what the workspace actually tracks.
 */
export function readmeVersion(
  versions: readonly ConfigVersion[]
): ConfigVersion | null {
  let latest: ConfigVersion | null = null;
  for (const version of versions) {
    if (version.status !== 'uploaded' || version.vcs?.pr_number) {
      continue;
    }
    if (latest === null || version.created_at > latest.created_at) {
      latest = version;
    }
  }
  return latest;
}

/** Matches a URL that names its own scheme, or one that is protocol relative. */
const ABSOLUTE = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i;

/** The schemes a link may keep. */
const SAFE_SCHEMES = new Set(['http:', 'https:', 'mailto:']);

/** The parent directory of a path inside the repository, with a trailing slash. */
function directoryOf(path: string): string {
  const slash = path.lastIndexOf('/');
  return slash === -1 ? '' : path.slice(0, slash + 1);
}

/** Encodes each segment of a repository path for a URL. */
function encodePath(path: string): string {
  return path.split('/').map(encodeURIComponent).join('/');
}

/** An absolute URL kept as written when its scheme is safe, else dropped. */
function keepAbsolute(url: string): string | null {
  try {
    const parsed = new URL(url, 'https://example.invalid/');
    return SAFE_SCHEMES.has(parsed.protocol) ? url : null;
  } catch {
    return null;
  }
}

/**
 * How a README's links and images resolve.
 *
 * Absolute URLs with a safe scheme are kept. Relative ones resolve against the
 * README's own place in the repository on GitHub, at the commit the version was
 * ingested from: links to the `blob` view, images to `raw`. A version with no
 * repository, an API upload, has nowhere to resolve them, so they are dropped
 * and the link text or image alt text stands alone.
 */
export function readmeResolvers(
  vcs: ConfigVersionVcs | null | undefined,
  readmePath: string
): { resolveHref: UrlResolver; resolveSrc: UrlResolver } {
  const resolve =
    (view: 'blob' | 'raw'): UrlResolver =>
    (url) => {
      const trimmed = url.trim();
      if (trimmed === '') {
        return null;
      }
      if (ABSOLUTE.test(trimmed)) {
        return keepAbsolute(trimmed);
      }
      if (!vcs) {
        return null;
      }
      const root = `https://github.com/${vcs.repo}/${view}/${encodeURIComponent(vcs.sha)}/`;
      let resolved: string;
      if (trimmed.startsWith('#')) {
        resolved = `${readmeSourceUrl(vcs, readmePath) ?? ''}${trimmed}`;
      } else if (trimmed.startsWith('/')) {
        resolved = new URL(trimmed.replace(/^\/+/, ''), root).toString();
      } else {
        resolved = new URL(
          trimmed,
          new URL(encodePath(directoryOf(readmePath)), root)
        ).toString();
      }
      return resolved.startsWith(root) ||
        resolved.startsWith(root.replace('/raw/', '/blob/'))
        ? resolved
        : null;
    };
  return { resolveHref: resolve('blob'), resolveSrc: resolve('raw') };
}

/** Where the README itself is viewed on GitHub, or null for an API upload. */
export function readmeSourceUrl(
  vcs: ConfigVersionVcs | null | undefined,
  readmePath: string
): string | null {
  if (!vcs) {
    return null;
  }
  return `https://github.com/${vcs.repo}/blob/${encodeURIComponent(vcs.sha)}/${encodePath(readmePath)}`;
}
