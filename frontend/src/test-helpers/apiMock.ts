/**
 * The typed stand-in for the module level `api` the pages import.
 *
 * A `vi.mock` factory sees an `any` shaped module, so the mocked methods are
 * declared here with the real signatures and the factory just returns this
 * object. That keeps each test file free of unsafe returns and gives the
 * mock methods their contract types.
 */

import { vi, type Mock } from 'vitest';

import type * as ApiClientModule from '../api/client';

type ApiClient = typeof ApiClientModule;
type TerraformApi = ApiClientModule.TerraformApi;

/** Every method a page may reach for, as a mock with the real signature. */
export type ApiMock = {
  [K in keyof TerraformApi]: TerraformApi[K] extends (
    ...args: infer TArgs
  ) => infer TResult
    ? Mock<(...args: TArgs) => TResult>
    : never;
};

/** A fresh mock of every API method, each rejecting until a test sets it. */
export function createApiMock(): ApiMock {
  const names: (keyof TerraformApi)[] = [
    'getAuthClient',
    'listWorkspaces',
    'createWorkspace',
    'getWorkspace',
    'updateWorkspace',
    'deleteWorkspace',
    'checkRunRole',
    'listVariables',
    'putVariable',
    'deleteVariable',
    'listConfigVersions',
    'createConfigVersion',
    'getConfigVersion',
    'listRuns',
    'createRun',
    'getRun',
    'confirmRun',
    'cancelRun',
    'discardRun',
    'getRunLogs',
  ];
  const mock = {} as Record<string, Mock>;
  for (const name of names) {
    mock[name] = vi.fn();
  }
  return mock as ApiMock;
}

/** The instance the `vi.mock` factories hand back in place of `api`. */
export const apiMock = createApiMock();

/** Clears every recorded call and implementation between tests. */
export function resetApiMock(): void {
  for (const value of Object.values(apiMock)) {
    (value as Mock).mockReset();
  }
}

/**
 * The module a `vi.mock('../api/client', ...)` factory returns: the real module
 * with `api` swapped for {@link apiMock}.
 *
 * The `importActual` lives here so no test file needs an inline `typeof import`
 * annotation, and every page test's factory is a one-liner.
 */
export async function apiClientModuleMock(): Promise<ApiClient> {
  const actual = await vi.importActual<ApiClient>('../api/client');
  return { ...actual, api: apiMock as unknown as ApiClient['api'] };
}
