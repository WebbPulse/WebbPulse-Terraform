import { describe, expect, it } from 'vitest';

import { aConfigVersion } from '../../test-helpers/fixtures';
import { readmeResolvers, readmeSourceUrl, readmeVersion } from './readme';

const VCS = { repo: 'acme/infra', sha: 'abc123', branch: 'main' };

describe('readmeVersion', () => {
  it('picks the newest uploaded version', () => {
    const older = aConfigVersion({
      config_version_id: 'cv-1',
      created_at: '2026-10-01T00:00:00Z',
    });
    const newer = aConfigVersion({
      config_version_id: 'cv-2',
      created_at: '2026-10-02T00:00:00Z',
    });
    expect(readmeVersion([older, newer])?.config_version_id).toBe('cv-2');
  });

  it('passes over pending versions and pull request versions', () => {
    const tracked = aConfigVersion({
      config_version_id: 'cv-1',
      created_at: '2026-10-01T00:00:00Z',
      source: 'vcs',
      vcs: VCS,
    });
    const pr = aConfigVersion({
      config_version_id: 'cv-2',
      created_at: '2026-10-02T00:00:00Z',
      source: 'vcs',
      vcs: { ...VCS, branch: null, pr_number: 7 },
    });
    const pending = aConfigVersion({
      config_version_id: 'cv-3',
      created_at: '2026-10-03T00:00:00Z',
      status: 'pending',
    });
    expect(readmeVersion([tracked, pr, pending])?.config_version_id).toBe(
      'cv-1'
    );
  });

  it('is null with nothing uploaded', () => {
    expect(readmeVersion([])).toBeNull();
  });
});

describe('readmeResolvers', () => {
  const { resolveHref, resolveSrc } = readmeResolvers(
    VCS,
    'stacks/app/README.md'
  );

  it('resolves relative links to the blob view beside the README', () => {
    expect(resolveHref('docs/usage.md')).toBe(
      'https://github.com/acme/infra/blob/abc123/stacks/app/docs/usage.md'
    );
    expect(resolveHref('../shared/README.md')).toBe(
      'https://github.com/acme/infra/blob/abc123/stacks/shared/README.md'
    );
  });

  it('resolves root relative links against the repository root', () => {
    expect(resolveHref('/LICENSE')).toBe(
      'https://github.com/acme/infra/blob/abc123/LICENSE'
    );
  });

  it('resolves anchors against the README itself', () => {
    expect(resolveHref('#inputs')).toBe(
      'https://github.com/acme/infra/blob/abc123/stacks/app/README.md#inputs'
    );
  });

  it('resolves images to the raw view', () => {
    expect(resolveSrc('diagram.png')).toBe(
      'https://github.com/acme/infra/raw/abc123/stacks/app/diagram.png'
    );
  });

  it('keeps safe absolute URLs and drops the rest', () => {
    expect(resolveHref('https://example.com/a')).toBe('https://example.com/a');
    expect(resolveHref('mailto:ops@example.com')).toBe(
      'mailto:ops@example.com'
    );
    expect(resolveHref('javascript:alert(1)')).toBeNull();
    expect(resolveSrc('data:image/svg+xml,<svg/>')).toBeNull();
  });

  it('drops a path that climbs out of the commit', () => {
    expect(resolveHref('../../../../../evil')).toBeNull();
  });

  it('drops relative URLs when there is no repository', () => {
    const api = readmeResolvers(null, 'README.md');
    expect(api.resolveHref('docs/usage.md')).toBeNull();
    expect(api.resolveSrc('diagram.png')).toBeNull();
    expect(api.resolveHref('https://example.com')).toBe('https://example.com');
  });
});

describe('readmeSourceUrl', () => {
  it('points at the README on GitHub', () => {
    expect(readmeSourceUrl(VCS, 'README.md')).toBe(
      'https://github.com/acme/infra/blob/abc123/README.md'
    );
  });

  it('is null for an API upload', () => {
    expect(readmeSourceUrl(null, 'README.md')).toBeNull();
  });
});
