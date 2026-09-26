/** Fixtures for the GitHub settings pages. */

import type { GitHubAppStatus, Installation } from '../../api';

/** An environment with no App yet. */
export function noApp(
  overrides: Partial<GitHubAppStatus> = {}
): GitHubAppStatus {
  return {
    configured: false,
    can_create: true,
    can_install: false,
    logo_path: '/github-app-logo.png',
    badge_background: '#4d9fff',
    ...overrides,
  };
}

/** A configured App owned by an organization. */
export function anApp(
  overrides: Partial<GitHubAppStatus> = {}
): GitHubAppStatus {
  return noApp({
    configured: true,
    can_create: false,
    can_install: true,
    slug: 'webbpulse-terraform-staging',
    app_id: '424242',
    name: 'webbpulse-terraform-staging',
    owner_login: 'WebbPulse',
    html_url: 'https://github.com/apps/webbpulse-terraform-staging',
    settings_url:
      'https://github.com/organizations/WebbPulse/settings/apps/webbpulse-terraform-staging',
    created_at: '2026-09-26T00:00:00Z',
    ...overrides,
  });
}

/** One installation on an organization. */
export function anInstallation(
  overrides: Partial<Installation> = {}
): Installation {
  return {
    installation_id: '77',
    account_login: 'WebbPulse',
    account_type: 'Organization',
    repository_selection: 'selected',
    html_url:
      'https://github.com/organizations/WebbPulse/settings/installations/77',
    suspended: false,
    installed_at: '2026-09-26T00:00:00Z',
    updated_at: '2026-09-26T00:00:00Z',
    ...overrides,
  };
}
