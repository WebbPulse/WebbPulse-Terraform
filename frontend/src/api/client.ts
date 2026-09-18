/** The typed client for the control plane API, and the auth client beside it. */

import {
  ApiError,
  createApiClient,
  getWebbPulseError,
  type ApiClient,
  type AuthTokenProvider,
  type RequestOptions,
} from '@webbpulse/api-client';
import { loadAppConfig } from '@webbpulse/config';
import { createAuthClient, type AuthClient } from '@webbpulse/auth';
import { identityOriginFrom as packageIdentityOriginFrom } from '@webbpulse/discovery';

import type {
  ConfigVersion,
  ConfigVersionList,
  ConfigVersionUpload,
  Run,
  RunCreate,
  RunList,
  RunLogPage,
  RunPhase,
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

/** Turns a thrown request error into a sentence a page can render. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return getWebbPulseError(error).message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return 'The request failed. Please try again.';
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

  /** Deletes a workspace. */
  async deleteWorkspace(
    workspaceId: string,
    options: RequestOptions = {}
  ): Promise<void> {
    await this.client.delete(
      `/workspaces/${encodeURIComponent(workspaceId)}`,
      options
    );
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
    body: { message?: string } = {},
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

  /** Lists runs, optionally scoped to one workspace. */
  async listRuns(
    query: { workspace_id?: string } = {},
    options: RequestOptions = {}
  ): Promise<RunList> {
    const response = await this.client.get<RunList>('/runs', {
      ...options,
      query: {
        ...options.query,
        ...(query.workspace_id === undefined
          ? {}
          : { workspace_id: query.workspace_id }),
      },
    });
    return response.data;
  }

  /** Starts a run. */
  async createRun(body: RunCreate, options: RequestOptions = {}): Promise<Run> {
    const response = await this.client.post<Run>('/runs', body, options);
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
  async confirmRun(
    runId: string,
    body: { comment?: string } = {},
    options: RequestOptions = {}
  ): Promise<Run> {
    const response = await this.client.post<Run>(
      `/runs/${encodeURIComponent(runId)}/confirm`,
      body,
      options
    );
    return response.data;
  }

  /** Cancels a run in flight or not yet started. */
  async cancelRun(
    runId: string,
    body: { comment?: string } = {},
    options: RequestOptions = {}
  ): Promise<Run> {
    const response = await this.client.post<Run>(
      `/runs/${encodeURIComponent(runId)}/cancel`,
      body,
      options
    );
    return response.data;
  }

  /** Discards a plan nobody will apply. */
  async discardRun(
    runId: string,
    body: { comment?: string } = {},
    options: RequestOptions = {}
  ): Promise<Run> {
    const response = await this.client.post<Run>(
      `/runs/${encodeURIComponent(runId)}/discard`,
      body,
      options
    );
    return response.data;
  }

  /**
   * Reads a page of a run's logs.
   *
   * `after` is the cursor the previous page returned, so the viewer tails the
   * stream instead of re-reading every line on each poll.
   */
  async getRunLogs(
    runId: string,
    query: { phase: RunPhase; after?: string | null },
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
}

/**
 * Uploads a configuration tarball to a presigned PUT.
 *
 * Deliberately not on {@link TerraformApi}: the URL is pre-signed for S3, so
 * the request must carry neither the API's auth header nor its cookies, and a
 * bare `fetch` is the only way to guarantee that.
 */
export async function uploadConfigTarball(
  upload: ConfigVersionUpload,
  file: Blob,
  options: { fetch?: typeof globalThis.fetch; signal?: AbortSignal } = {}
): Promise<void> {
  const doFetch = options.fetch ?? globalThis.fetch;
  const response = await doFetch(upload.upload_url, {
    method: 'PUT',
    body: file,
    headers: upload.upload_headers ?? {},
    ...(options.signal === undefined ? {} : { signal: options.signal }),
  });
  if (!response.ok) {
    throw new Error(
      `Uploading the configuration tarball failed with ${String(response.status)}.`
    );
  }
}

/** The API instance the pages use. */
export const api = new TerraformApi();
