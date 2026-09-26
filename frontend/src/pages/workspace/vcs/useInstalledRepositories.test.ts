import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../../../test-helpers/apiMock';
import { anApp, anInstallation, noApp } from '../../settings/fixtures';

vi.mock('../../../api/client', () => apiClientModuleMock());

const { loadInstalledRepositories } =
  await import('./useInstalledRepositories');

describe('loadInstalledRepositories', () => {
  beforeEach(() => {
    resetApiMock();
  });

  it('asks for the installations without waiting on the App status', async () => {
    let resolveApp: (value: ReturnType<typeof anApp>) => void = () => undefined;
    apiMock.getGitHubApp.mockReturnValue(
      new Promise((resolve) => {
        resolveApp = resolve;
      })
    );
    apiMock.listGitHubInstallations.mockResolvedValue({
      items: [
        anInstallation(),
        anInstallation({ installation_id: '2', account_login: 'other' }),
      ],
    });
    apiMock.listGitHubRepositories.mockImplementation((id: string) =>
      Promise.resolve({
        items: [
          {
            id: Number(id),
            name: id === '2' ? 'alpha' : 'zeta',
            full_name: id === '2' ? 'other/alpha' : 'WebbPulse/zeta',
            private: false,
            default_branch: 'main',
          },
        ],
      })
    );

    const loading = loadInstalledRepositories(new AbortController().signal);

    expect(apiMock.listGitHubInstallations).toHaveBeenCalledTimes(1);
    resolveApp(anApp());
    const result = await loading;

    expect(result.configured).toBe(true);
    expect(result.installations).toBe(2);
    expect(result.repositories.map((r) => r.full_name)).toEqual([
      'other/alpha',
      'WebbPulse/zeta',
    ]);
  });

  it('reports no App without failing on the installations read', async () => {
    apiMock.getGitHubApp.mockResolvedValue(noApp());
    apiMock.listGitHubInstallations.mockRejectedValue(new Error('no app'));

    await expect(
      loadInstalledRepositories(new AbortController().signal)
    ).resolves.toEqual({
      configured: false,
      installations: 0,
      repositories: [],
    });
    expect(apiMock.listGitHubRepositories).not.toHaveBeenCalled();
  });
});
