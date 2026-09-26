/** The typed client for the control plane API, and the auth client beside it. */

import {
  createApiClient,
  type ApiClient,
  type AuthTokenProvider,
  type RequestOptions,
} from '@webbpulse/api-client';
import { loadAppConfig } from '@webbpulse/config';
import {
  createAuthClient,
  describeAuthError,
  type AuthClient,
} from '@webbpulse/auth';
import { identityOriginFrom as packageIdentityOriginFrom } from '@webbpulse/auth/browser';

import type {
  ConfigVersion,
  ConfigVersionCreate,
  ConfigVersionList,
  ConfigVersionUpload,
  GitHubAppStatus,
  Installation,
  InstallationCallback,
  InstallationList,
  InstallStart,
  ManifestConversionRequest,
  ManifestStart,
  ManifestStartRequest,
  RepositoryList,
  Run,
  RunCreate,
  RunCreated,
  RunList,
  RunListQuery,
  RunLogPage,
  RunLogsQuery,
  RunPhase,
  RunPlan,
  RunRoleCheck,
  Variable,
  VariableList,
  VariableWrite,
  Workspace,
  WorkspaceCreate,
  WorkspaceList,
  WorkspaceUpdate,
} from './types';

const config = loadAppConfig(import.meta.env, {
  defaultApiBaseUrl:
    import.meta.env.MODE === 'production'
      ? 'https://api.terraform.webbpulse.com/api/v1'
      : 'http://localhost:8000/api/v1',
  defaultAppName: 'WebbPulse Terraform',
});

/** Base URL of the control plane API, from the resolved app config. */
export const API_BASE_URL = config.apiBaseUrl;

/**
 * The origin the identity routes hang off, derived from the API base URL.
 *
 * Identity mounts at `/api/auth` on the origin while the control plane's own
 * routes live under `/api/v1`, and `passthrough` leaves a root relative base
 * alone so the dev proxy keeps working.
 */
export function identityOriginFrom(apiBaseUrl: string): string {
  return packageIdentityOriginFrom(apiBaseUrl, { relativeAs: 'passthrough' });
}

/**
 * Turns a thrown request error into a sentence a page can render.
 *
 * `describeAuthError` unwraps the WebbPulse envelope the control plane and the
 * identity routes both answer with, so one renderer covers a failed plan and a
 * refused sign-in alike. The fallback is this product's own wording.
 */
export function describeError(error: unknown): string {
  return describeAuthError(error, 'The request failed. Please try again.');
}

/** Options for {@link TerraformApi}. */
export interface TerraformApiOptions {
  /** Base URL of the API. Defaults to the resolved app config's. */
  baseUrl?: string;
  /** Injected for tests. Defaults to `globalThis.fetch`. */
  fetch?: typeof globalThis.fetch;
  /** Retry attempts for idempotent methods. Defaults to the client's own. */
  retries?: number;
}

/**
 * The control plane API, one method per contract route.
 *
 * The transport is `@webbpulse/api-client`, which rejects on a non-2xx, so
 * these methods reject too and the pages surface the failure through
 * `usePolledQuery` and `useMutationWithRefetch` rather than an envelope.
 */
export class TerraformApi {
  private readonly client: ApiClient;

  /** The auth client holding the access token and spending the refresh cookie. */
  private readonly auth: AuthClient<unknown>;

  constructor(options: TerraformApiOptions = {}) {
    const baseUrl = options.baseUrl ?? API_BASE_URL;
    const credentials = 'include' as const;

    this.auth = createAuthClient({
      baseUrl: identityOriginFrom(baseUrl),
      clientOptions: {
        credentials,
        ...(options.fetch === undefined ? {} : { fetch: options.fetch }),
        ...(options.retries === undefined ? {} : { retries: options.retries }),
      },
    });
    this.client = createApiClient({
      baseUrl,
      credentials,
      auth: this.auth satisfies AuthTokenProvider,
      ...(options.fetch === undefined ? {} : { fetch: options.fetch }),
      ...(options.retries === undefined ? {} : { retries: options.retries }),
    });
  }

  /**
   * The auth client.
   *
   * Exposed so `AuthProvider` shares the instance the API client refreshes
   * through, rather than a second one with its own token.
   */
  getAuthClient(): AuthClient<unknown> {
    return this.auth;
  }

  /** Lists workspaces. */
  async listWorkspaces(options: RequestOptions = {}): Promise<WorkspaceList> {
    const response = await this.client.get<WorkspaceList>(
      '/workspaces',
      options
    );
    return response.data;
  }

  /** Creates a workspace. */
  async createWorkspace(
    body: WorkspaceCreate,
    options: RequestOptions = {}
  ): Promise<Workspace> {
    const response = await this.client.post<Workspace>(
      '/workspaces',
      body,
      options
    );
    return response.data;
  }

  /** Reads one workspace. */
  async getWorkspace(
    workspaceId: string,
    options: RequestOptions = {}
  ): Promise<Workspace> {
    const response = await this.client.get<Workspace>(
      `/workspaces/${encodeURIComponent(workspaceId)}`,
      options
    );
    return response.data;
  }

  /** Edits a workspace. */
  async updateWorkspace(
    workspaceId: string,
    body: WorkspaceUpdate,
    options: RequestOptions = {}
  ): Promise<Workspace> {
    const response = await this.client.patch<Workspace>(
      `/workspaces/${encodeURIComponent(workspaceId)}`,
      body,
      options
    );
    return response.data;
  }

  /**
   * Deletes a workspace. A safe delete rejects with `WORKSPACE_MANAGES_RESOURCES`
   * while state tracks resources, and `force` skips that check. Either rejects
   * with `WORKSPACE_HAS_ACTIVE_RUN` while a run is unfinished.
   */
  async deleteWorkspace(
    workspaceId: string,
    { force = false }: { force?: boolean } = {},
    options: RequestOptions = {}
  ): Promise<void> {
    await this.client.delete(
      `/workspaces/${encodeURIComponent(workspaceId)}`,
      force
        ? { ...options, query: { ...options.query, force: 'true' } }
        : options
    );
  }

  /**
   * Reads whether the runner has assumed the workspace's run role, writing
   * nothing. Rejects with `RUN_ROLE_MISSING` when no role is set.
   */
  async readRunRoleCheck(
    workspaceId: string,
    options: RequestOptions = {}
  ): Promise<RunRoleCheck> {
    const response = await this.client.get<RunRoleCheck>(
      `/workspaces/${encodeURIComponent(workspaceId)}/run-role/check`,
      options
    );
    return response.data;
  }

  /**
   * The same answer as `readRunRoleCheck`, recorded on the workspace so the
   * workspace list shows it. Rejects with `RUN_ROLE_MISSING` when no role is set.
   */
  async checkRunRole(
    workspaceId: string,
    options: RequestOptions = {}
  ): Promise<RunRoleCheck> {
    const response = await this.client.post<RunRoleCheck>(
      `/workspaces/${encodeURIComponent(workspaceId)}/run-role/check`,
      undefined,
      options
    );
    return response.data;
  }

  /** Lists a workspace's variables. Sensitive ones carry no value. */
  async listVariables(
    workspaceId: string,
    options: RequestOptions = {}
  ): Promise<VariableList> {
    const response = await this.client.get<VariableList>(
      `/workspaces/${encodeURIComponent(workspaceId)}/variables`,
      options
    );
    return response.data;
  }

  /** Creates or replaces one variable. */
  async putVariable(
    workspaceId: string,
    key: string,
    body: VariableWrite,
    options: RequestOptions = {}
  ): Promise<Variable> {
    const response = await this.client.put<Variable>(
      `/workspaces/${encodeURIComponent(workspaceId)}/variables/${encodeURIComponent(key)}`,
      body,
      options
    );
    return response.data;
  }

  /** Deletes one variable. */
  async deleteVariable(
    workspaceId: string,
    key: string,
    options: RequestOptions = {}
  ): Promise<void> {
    await this.client.delete(
      `/workspaces/${encodeURIComponent(workspaceId)}/variables/${encodeURIComponent(key)}`,
      options
    );
  }

  /** Lists a workspace's configuration versions. */
  async listConfigVersions(
    workspaceId: string,
    options: RequestOptions = {}
  ): Promise<ConfigVersionList> {
    const response = await this.client.get<ConfigVersionList>(
      `/workspaces/${encodeURIComponent(workspaceId)}/config-versions`,
      options
    );
    return response.data;
  }

  /**
   * Creates a configuration version and returns the presigned PUT to upload
   * its tarball to. The upload itself is {@link uploadConfigTarball}.
   */
  async createConfigVersion(
    workspaceId: string,
    body: ConfigVersionCreate = {},
    options: RequestOptions = {}
  ): Promise<ConfigVersionUpload> {
    const response = await this.client.post<ConfigVersionUpload>(
      `/workspaces/${encodeURIComponent(workspaceId)}/config-versions`,
      body,
      options
    );
    return response.data;
  }

  /** Reads one configuration version. */
  async getConfigVersion(
    workspaceId: string,
    configVersionId: string,
    options: RequestOptions = {}
  ): Promise<ConfigVersion> {
    const response = await this.client.get<ConfigVersion>(
      `/workspaces/${encodeURIComponent(workspaceId)}/config-versions/${encodeURIComponent(configVersionId)}`,
      options
    );
    return response.data;
  }

  /**
   * Lists runs, newest first, in one workspace or across every workspace.
   *
   * Naming a workspace returns all of its runs and answers 404 for a workspace
   * that does not exist. Omitting it returns one page of every workspace's runs;
   * `limit` and `cursor` apply only then, with `next_cursor` continuing the list.
   */
  async listRuns(
    query: RunListQuery = {},
    options: RequestOptions = {}
  ): Promise<RunList> {
    const scope: Record<string, string | number> = {};
    if (query.workspace_id !== undefined && query.workspace_id !== null) {
      scope['workspace_id'] = query.workspace_id;
    }
    if (query.limit !== undefined && query.limit !== null) {
      scope['limit'] = query.limit;
    }
    if (query.cursor !== undefined && query.cursor !== null) {
      scope['cursor'] = query.cursor;
    }
    const response = await this.client.get<RunList>('/runs', {
      ...options,
      query: { ...options.query, ...scope },
    });
    return response.data;
  }

  /** Starts a run. */
  async createRun(
    body: RunCreate,
    options: RequestOptions = {}
  ): Promise<RunCreated> {
    const response = await this.client.post<RunCreated>('/runs', body, options);
    return response.data;
  }

  /** Reads one run. */
  async getRun(runId: string, options: RequestOptions = {}): Promise<Run> {
    const response = await this.client.get<Run>(
      `/runs/${encodeURIComponent(runId)}`,
      options
    );
    return response.data;
  }

  /** Confirms a planned run, which starts its apply. */
  async confirmRun(runId: string, options: RequestOptions = {}): Promise<Run> {
    const response = await this.client.post<Run>(
      `/runs/${encodeURIComponent(runId)}/confirm`,
      undefined,
      options
    );
    return response.data;
  }

  /** Cancels a run in flight or not yet started. */
  async cancelRun(runId: string, options: RequestOptions = {}): Promise<Run> {
    const response = await this.client.post<Run>(
      `/runs/${encodeURIComponent(runId)}/cancel`,
      undefined,
      options
    );
    return response.data;
  }

  /** Discards a plan nobody will apply. */
  async discardRun(runId: string, options: RequestOptions = {}): Promise<Run> {
    const response = await this.client.post<Run>(
      `/runs/${encodeURIComponent(runId)}/discard`,
      undefined,
      options
    );
    return response.data;
  }

  /**
   * Reads one run's plan, summarised and redacted for the viewer.
   *
   * A 404 covers both an absent run and a run whose plan JSON has not been
   * uploaded yet, so a caller polling a run that is still planning treats it as
   * not ready rather than as a failure.
   */
  async getRunPlan(
    runId: string,
    options: RequestOptions = {}
  ): Promise<RunPlan> {
    const response = await this.client.get<RunPlan>(
      `/runs/${encodeURIComponent(runId)}/plan`,
      options
    );
    return response.data;
  }

  /**
   * Reads a page of a run's logs.
   *
   * `after` is the cursor the previous page returned, so the viewer tails the
   * stream instead of re-reading every line on each poll.
   *
   * `phase` is narrowed to {@link RunPhase}. The route declares it as a pattern
   * constrained string rather than a literal, so the generated query type is a
   * bare `string` and a caller could otherwise pass a phase the route rejects.
   */
  async getRunLogs(
    runId: string,
    query: Omit<RunLogsQuery, 'phase'> & { phase: RunPhase },
    options: RequestOptions = {}
  ): Promise<RunLogPage> {
    const response = await this.client.get<RunLogPage>(
      `/runs/${encodeURIComponent(runId)}/logs`,
      {
        ...options,
        query: {
          ...options.query,
          phase: query.phase,
          ...(query.after === undefined || query.after === null
            ? {}
            : { after: query.after }),
        },
      }
    );
    return response.data;
  }

  /** Reads whether this environment has a GitHub App. Admin only. */
  async getGitHubApp(options: RequestOptions = {}): Promise<GitHubAppStatus> {
    const response = await this.client.get<GitHubAppStatus>(
      '/github/app',
      options
    );
    return response.data;
  }

  /**
   * Issues a one-time state and the manifest to post to GitHub. Rejects with
   * `GITHUB_APP_ALREADY_CONFIGURED` when an App exists.
   */
  async startGitHubManifest(
    body: ManifestStartRequest = {},
    options: RequestOptions = {}
  ): Promise<ManifestStart> {
    const response = await this.client.post<ManifestStart>(
      '/github/app/manifest',
      body,
      options
    );
    return response.data;
  }

  /** Exchanges the create callback's code, storing the App's credentials. */
  async convertGitHubManifest(
    body: ManifestConversionRequest,
    options: RequestOptions = {}
  ): Promise<GitHubAppStatus> {
    const response = await this.client.post<GitHubAppStatus>(
      '/github/app/conversions',
      body,
      options
    );
    return response.data;
  }

  /** Issues a one-time state and the App's install URL. */
  async startGitHubInstall(
    options: RequestOptions = {}
  ): Promise<InstallStart> {
    const response = await this.client.post<InstallStart>(
      '/github/install-state',
      undefined,
      options
    );
    return response.data;
  }

  /** Records the installation the setup callback named, once GitHub confirms it. */
  async recordGitHubInstallation(
    body: InstallationCallback,
    options: RequestOptions = {}
  ): Promise<Installation> {
    const response = await this.client.post<Installation>(
      '/github/installations',
      body,
      options
    );
    return response.data;
  }

  /** Lists the stored installations. */
  async listGitHubInstallations(
    options: RequestOptions = {}
  ): Promise<InstallationList> {
    const response = await this.client.get<InstallationList>(
      '/github/installations',
      options
    );
    return response.data;
  }

  /** Re-reads one installation from GitHub, dropping it when GitHub no longer has it. */
  async refreshGitHubInstallation(
    installationId: string,
    options: RequestOptions = {}
  ): Promise<Installation> {
    const response = await this.client.post<Installation>(
      `/github/installations/${encodeURIComponent(installationId)}/refresh`,
      undefined,
      options
    );
    return response.data;
  }

  /** Forgets one installation here. It stays installed on GitHub. */
  async removeGitHubInstallation(
    installationId: string,
    options: RequestOptions = {}
  ): Promise<void> {
    await this.client.delete(
      `/github/installations/${encodeURIComponent(installationId)}`,
      options
    );
  }

  /** Lists the repositories one installation can reach. */
  async listGitHubRepositories(
    installationId: string,
    options: RequestOptions = {}
  ): Promise<RepositoryList> {
    const response = await this.client.get<RepositoryList>(
      `/github/installations/${encodeURIComponent(installationId)}/repositories`,
      options
    );
    return response.data;
  }
}

/**
 * Uploads a configuration tarball to a presigned PUT.
 *
 * Deliberately not on {@link TerraformApi}: the URL is pre-signed for S3, so
 * the request must carry neither the API's auth header nor its cookies, and a
 * bare `fetch` is the only way to guarantee that.
 *
 * Every header the API returned is sent verbatim. They are signed into the
 * URL, so dropping or changing one makes S3 reject the PUT.
 */
export async function uploadConfigTarball(
  upload: ConfigVersionUpload,
  file: Blob,
  options: { fetch?: typeof globalThis.fetch; signal?: AbortSignal } = {}
): Promise<void> {
  const doFetch = options.fetch ?? globalThis.fetch;
  let response: Response;
  try {
    response = await doFetch(upload.upload_url, {
      method: 'PUT',
      body: file,
      headers: upload.headers,
      ...(options.signal === undefined ? {} : { signal: options.signal }),
    });
  } catch (thrown) {
    if (options.signal?.aborted === true) {
      throw thrown;
    }
    throw new Error(
      'The configuration tarball could not be sent to storage. Check the connection and try again.',
      { cause: thrown }
    );
  }
  if (!response.ok) {
    throw new Error(
      `Uploading the configuration tarball failed with ${String(response.status)}.`
    );
  }
}

/** The API instance the pages use. */
export const api = new TerraformApi();
