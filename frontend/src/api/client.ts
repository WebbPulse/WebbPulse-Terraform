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
  StepUpCancelledError,
  type AuthClient,
  type WebAuthnAdapter,
} from '@webbpulse/auth';
import { identityOriginFrom as packageIdentityOriginFrom } from '@webbpulse/auth/browser';

import type {
  ApiKey,
  ApiKeyCreate,
  ApiKeyCreated,
  ApiKeyList,
  ConfigVersionCreate,
  ConfigVersionDetail,
  ConfigVersionList,
  ConfigVersionUpload,
  GitHubAppStatus,
  Installation,
  InstallationCallback,
  InstallationList,
  InstallStart,
  LoginAuthorization,
  LoginAuthorizationCreate,
  Module,
  ModuleCreate,
  ModuleList,
  ModuleSync,
  ModuleVersionDetail,
  NotificationConfiguration,
  NotificationConfigurationCreate,
  NotificationConfigurationList,
  NotificationConfigurationUpdate,
  NotificationDelivery,
  Project,
  ProjectCreate,
  ProjectList,
  ProjectUpdate,
  ManifestConversionRequest,
  ManifestStart,
  ManifestStartRequest,
  Provider,
  ProviderCreate,
  ProviderList,
  ProviderSync,
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
  RunRoleQuickSetup,
  RunRoleQuickSetupCreate,
  Variable,
  VariableList,
  VariableWrite,
  Workspace,
  WorkspaceCreate,
  WorkspaceList,
  WorkspaceListQuery,
  WebhookConfig,
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
 * refused sign-in alike. A dismissed step-up prompt says nothing changed. The
 * fallback is this product's own wording.
 */
export function describeError(error: unknown): string {
  if (error instanceof StepUpCancelledError) {
    return 'You did not confirm it is you, so nothing changed.';
  }
  return describeAuthError(error, 'The request failed. Please try again.');
}

/**
 * The shape of `withStepUp` from `useStepUp`: wraps a call so a
 * `STEP_UP_REQUIRED` refusal prompts in place and replays it once.
 */
export type StepUpWrapper = <TArgs extends unknown[], TResult>(
  fn: (...args: TArgs) => Promise<TResult>
) => (...args: TArgs) => Promise<TResult>;

/** The gate used while no step-up prompt is mounted: the call as it is. */
const passThrough: StepUpWrapper = (fn) => fn;

/** Options for {@link TerraformApi}. */
export interface TerraformApiOptions {
  /** Base URL of the API. Defaults to the resolved app config's. */
  baseUrl?: string;
  /** Injected for tests. Defaults to `globalThis.fetch`. */
  fetch?: typeof globalThis.fetch;
  /** Retry attempts for idempotent methods. Defaults to the client's own. */
  retries?: number;
  /** Injected for tests. Defaults to `navigator.credentials`. */
  webAuthn?: WebAuthnAdapter;
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

  /** The wrapper gated calls go through; a pass through until a prompt mounts. */
  private stepUpGate: StepUpWrapper = passThrough;

  constructor(options: TerraformApiOptions = {}) {
    const baseUrl = options.baseUrl ?? API_BASE_URL;
    const credentials = 'include' as const;

    this.auth = createAuthClient({
      baseUrl: identityOriginFrom(baseUrl),
      ...(options.webAuthn === undefined ? {} : { webAuthn: options.webAuthn }),
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
   * Routes every step-up gated call through `gate`, the `withStepUp` of the one
   * mounted prompt, so a `STEP_UP_REQUIRED` refusal asks for a passkey, code or password and
   * replays the call once. Returns a function that detaches the gate again.
   */
  setStepUpGate(gate: StepUpWrapper): () => void {
    this.stepUpGate = gate;
    return () => {
      if (this.stepUpGate === gate) {
        this.stepUpGate = passThrough;
      }
    };
  }

  /** Sends a step-up gated call through the mounted prompt, if there is one. */
  private sudo<T>(call: () => Promise<T>): Promise<T> {
    return this.stepUpGate(call)();
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

  /** Lists workspaces, optionally one project's, matching a search, in a sort. */
  async listWorkspaces(
    options: RequestOptions = {},
    query: WorkspaceListQuery = {}
  ): Promise<WorkspaceList> {
    const scope: Record<string, string> = {};
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') {
        scope[key] = String(value);
      }
    }
    const response = await this.client.get<WorkspaceList>(
      '/workspaces',
      Object.keys(scope).length === 0
        ? options
        : { ...options, query: { ...options.query, ...scope } }
    );
    return response.data;
  }

  /** Lists every project, the default first. */
  async listProjects(options: RequestOptions = {}): Promise<ProjectList> {
    const response = await this.client.get<ProjectList>('/projects', options);
    return response.data;
  }

  /** Reads one project. `prj-default` is the default project. */
  async getProject(
    projectId: string,
    options: RequestOptions = {}
  ): Promise<Project> {
    const response = await this.client.get<Project>(
      `/projects/${encodeURIComponent(projectId)}`,
      options
    );
    return response.data;
  }

  /** Creates a project. Rejects with `PROJECT_NAME_TAKEN` for a name in use. */
  async createProject(
    body: ProjectCreate,
    options: RequestOptions = {}
  ): Promise<Project> {
    const response = await this.client.post<Project>(
      '/projects',
      body,
      options
    );
    return response.data;
  }

  /** Renames a project or changes its description. */
  async updateProject(
    projectId: string,
    body: ProjectUpdate,
    options: RequestOptions = {}
  ): Promise<Project> {
    const response = await this.client.patch<Project>(
      `/projects/${encodeURIComponent(projectId)}`,
      body,
      options
    );
    return response.data;
  }

  /** Deletes an empty project. Rejects with `PROJECT_NOT_EMPTY` otherwise. */
  async deleteProject(
    projectId: string,
    options: RequestOptions = {}
  ): Promise<void> {
    await this.client.delete(
      `/projects/${encodeURIComponent(projectId)}`,
      options
    );
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
    const response = await this.sudo(() =>
      this.client.patch<Workspace>(
        `/workspaces/${encodeURIComponent(workspaceId)}`,
        body,
        options
      )
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
    await this.sudo(() =>
      this.client.delete(
        `/workspaces/${encodeURIComponent(workspaceId)}`,
        force
          ? { ...options, query: { ...options.query, force: 'true' } }
          : options
      )
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

  /**
   * Saves the role ARN derived from `body.account_id` and returns the AWS
   * CloudFormation quick create link that creates the role.
   */
  async startRunRoleQuickSetup(
    workspaceId: string,
    body: RunRoleQuickSetupCreate,
    options: RequestOptions = {}
  ): Promise<RunRoleQuickSetup> {
    const response = await this.sudo(() =>
      this.client.post<RunRoleQuickSetup>(
        `/workspaces/${encodeURIComponent(workspaceId)}/run-role/quick-setup`,
        body,
        options
      )
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
    const response = await this.sudo(() =>
      this.client.put<Variable>(
        `/workspaces/${encodeURIComponent(workspaceId)}/variables/${encodeURIComponent(key)}`,
        body,
        options
      )
    );
    return response.data;
  }

  /** Deletes one variable. */
  async deleteVariable(
    workspaceId: string,
    key: string,
    options: RequestOptions = {}
  ): Promise<void> {
    await this.sudo(() =>
      this.client.delete(
        `/workspaces/${encodeURIComponent(workspaceId)}/variables/${encodeURIComponent(key)}`,
        options
      )
    );
  }

  /** Lists a workspace's notification configurations, each with its last delivery. */
  async listNotificationConfigurations(
    workspaceId: string,
    options: RequestOptions = {}
  ): Promise<NotificationConfigurationList> {
    const response = await this.client.get<NotificationConfigurationList>(
      `/workspaces/${encodeURIComponent(workspaceId)}/notification-configurations`,
      options
    );
    return response.data;
  }

  /** Creates a notification configuration. Rejects with a 409 past 50 on one workspace. */
  async createNotificationConfiguration(
    workspaceId: string,
    body: NotificationConfigurationCreate,
    options: RequestOptions = {}
  ): Promise<NotificationConfiguration> {
    const response = await this.sudo(() =>
      this.client.post<NotificationConfiguration>(
        `/workspaces/${encodeURIComponent(workspaceId)}/notification-configurations`,
        body,
        options
      )
    );
    return response.data;
  }

  /** Edits a notification configuration. An absent field is unchanged. */
  async updateNotificationConfiguration(
    workspaceId: string,
    notificationId: string,
    body: NotificationConfigurationUpdate,
    options: RequestOptions = {}
  ): Promise<NotificationConfiguration> {
    const response = await this.sudo(() =>
      this.client.patch<NotificationConfiguration>(
        `/workspaces/${encodeURIComponent(workspaceId)}/notification-configurations/${encodeURIComponent(notificationId)}`,
        body,
        options
      )
    );
    return response.data;
  }

  /** Deletes a notification configuration. */
  async deleteNotificationConfiguration(
    workspaceId: string,
    notificationId: string,
    options: RequestOptions = {}
  ): Promise<void> {
    await this.sudo(() =>
      this.client.delete(
        `/workspaces/${encodeURIComponent(workspaceId)}/notification-configurations/${encodeURIComponent(notificationId)}`,
        options
      )
    );
  }

  /**
   * Sends a test delivery now and returns its outcome. A receiver's refusal comes
   * back as a `failed` delivery, and a 429 carries `Retry-After`.
   */
  async verifyNotificationConfiguration(
    workspaceId: string,
    notificationId: string,
    options: RequestOptions = {}
  ): Promise<NotificationDelivery> {
    const response = await this.sudo(() =>
      this.client.post<NotificationDelivery>(
        `/workspaces/${encodeURIComponent(workspaceId)}/notification-configurations/${encodeURIComponent(notificationId)}/actions/verify`,
        undefined,
        options
      )
    );
    return response.data;
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

  /** Reads one configuration version, with the README the overview shows. */
  async getConfigVersion(
    workspaceId: string,
    configVersionId: string,
    options: RequestOptions = {}
  ): Promise<ConfigVersionDetail> {
    const response = await this.client.get<ConfigVersionDetail>(
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
    if (query.project_id !== undefined && query.project_id !== null) {
      scope['project_id'] = query.project_id;
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

  /**
   * Confirms a planned run, which starts its apply, with an optional comment kept on the run.
   *
   * Not step-up gated: `runs:apply` on a live session is enough.
   */
  async confirmRun(
    runId: string,
    comment = '',
    options: RequestOptions = {}
  ): Promise<Run> {
    const response = await this.client.post<Run>(
      `/runs/${encodeURIComponent(runId)}/confirm`,
      comment.trim() === '' ? undefined : { comment },
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

  /** Discards a plan nobody will apply, with an optional comment kept on the run. */
  async discardRun(
    runId: string,
    comment = '',
    options: RequestOptions = {}
  ): Promise<Run> {
    const response = await this.client.post<Run>(
      `/runs/${encodeURIComponent(runId)}/discard`,
      comment.trim() === '' ? undefined : { comment },
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
    const response = await this.sudo(() =>
      this.client.post<ManifestStart>('/github/app/manifest', body, options)
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

  /**
   * Points the App's webhook at this API and sets its signing secret. Rejects
   * with `GITHUB_WEBHOOK_URL_MISSING` or `GITHUB_WEBHOOK_SECRET_MISSING` when
   * the environment lacks either.
   */
  async syncGitHubWebhook(
    options: RequestOptions = {}
  ): Promise<WebhookConfig> {
    const response = await this.sudo(() =>
      this.client.post<WebhookConfig>('/github/app/webhook', undefined, options)
    );
    return response.data;
  }

  /** Issues a one-time state and the App's install URL. */
  async startGitHubInstall(
    options: RequestOptions = {}
  ): Promise<InstallStart> {
    const response = await this.sudo(() =>
      this.client.post<InstallStart>(
        '/github/install-state',
        undefined,
        options
      )
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
    await this.sudo(() =>
      this.client.delete(
        `/github/installations/${encodeURIComponent(installationId)}`,
        options
      )
    );
  }

  /** Lists the caller's own API keys, newest first, revoked and expired ones included. */
  async listApiKeys(options: RequestOptions = {}): Promise<ApiKeyList> {
    const response = await this.client.get<ApiKeyList>('/api-keys', options);
    return response.data;
  }

  /** Mints an API key. The response is the only time its plaintext is readable. */
  async createApiKey(
    body: ApiKeyCreate,
    options: RequestOptions = {}
  ): Promise<ApiKeyCreated> {
    const response = await this.sudo(() =>
      this.client.post<ApiKeyCreated>('/api-keys', body, options)
    );
    return response.data;
  }

  /** Revokes one API key by its id, returning it as it was. */
  async revokeApiKey(
    keyId: string,
    options: RequestOptions = {}
  ): Promise<ApiKey> {
    const response = await this.sudo(() =>
      this.client.delete<ApiKey>(
        `/api-keys/${encodeURIComponent(keyId)}`,
        options
      )
    );
    return response.data;
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

  /** Lists every private registry module with its versions. */
  async listModules(options: RequestOptions = {}): Promise<ModuleList> {
    const response = await this.client.get<ModuleList>(
      '/registry/modules',
      options
    );
    return response.data;
  }

  /** Connects a module to a repository the GitHub App is installed on. */
  async createModule(
    body: ModuleCreate,
    options: RequestOptions = {}
  ): Promise<Module> {
    const response = await this.sudo(() =>
      this.client.post<Module>('/registry/modules', body, options)
    );
    return response.data;
  }

  /** Reads one module and every version of it. */
  async getModule(
    namespace: string,
    name: string,
    provider: string,
    options: RequestOptions = {}
  ): Promise<Module> {
    const response = await this.client.get<Module>(
      modulePath(namespace, name, provider),
      options
    );
    return response.data;
  }

  /** Reads one version of a module with its documentation. */
  async getModuleVersion(
    namespace: string,
    name: string,
    provider: string,
    version: string,
    options: RequestOptions = {}
  ): Promise<ModuleVersionDetail> {
    const response = await this.client.get<ModuleVersionDetail>(
      `${modulePath(namespace, name, provider)}/versions/${encodeURIComponent(version)}`,
      options
    );
    return response.data;
  }

  /** Queues an import of every semantic version tag in the module's repository. */
  async resyncModule(
    namespace: string,
    name: string,
    provider: string,
    options: RequestOptions = {}
  ): Promise<ModuleSync> {
    const response = await this.client.post<ModuleSync>(
      `${modulePath(namespace, name, provider)}/resync`,
      undefined,
      options
    );
    return response.data;
  }

  /** Removes a module with every version. Configurations pinned to it stop resolving. */
  async deleteModule(
    namespace: string,
    name: string,
    provider: string,
    options: RequestOptions = {}
  ): Promise<void> {
    await this.sudo(() =>
      this.client.delete(modulePath(namespace, name, provider), options)
    );
  }

  /** Lists every private registry provider with its versions. */
  async listProviders(options: RequestOptions = {}): Promise<ProviderList> {
    const response = await this.client.get<ProviderList>(
      '/registry/providers',
      options
    );
    return response.data;
  }

  /** Connects a provider to a repository the GitHub App is installed on. */
  async createProvider(
    body: ProviderCreate,
    options: RequestOptions = {}
  ): Promise<Provider> {
    const response = await this.sudo(() =>
      this.client.post<Provider>('/registry/providers', body, options)
    );
    return response.data;
  }

  /** Reads one provider and every version of it. */
  async getProvider(
    namespace: string,
    type: string,
    options: RequestOptions = {}
  ): Promise<Provider> {
    const response = await this.client.get<Provider>(
      providerPath(namespace, type),
      options
    );
    return response.data;
  }

  /** Queues an import of every release in the provider's repository. */
  async resyncProvider(
    namespace: string,
    type: string,
    options: RequestOptions = {}
  ): Promise<ProviderSync> {
    const response = await this.client.post<ProviderSync>(
      `${providerPath(namespace, type)}/resync`,
      undefined,
      options
    );
    return response.data;
  }

  /** Removes a provider with every version. Configurations requiring it stop installing. */
  async deleteProvider(
    namespace: string,
    type: string,
    options: RequestOptions = {}
  ): Promise<void> {
    await this.sudo(() =>
      this.client.delete(providerPath(namespace, type), options)
    );
  }

  /** Approves a `terraform login` request and returns the loopback redirect. */
  async createTerraformLoginAuthorization(
    body: LoginAuthorizationCreate,
    options: RequestOptions = {}
  ): Promise<LoginAuthorization> {
    const response = await this.sudo(() =>
      this.client.post<LoginAuthorization>(
        '/oauth/authorizations',
        body,
        options
      )
    );
    return response.data;
  }
}

/** The API path of one provider. */
function providerPath(namespace: string, type: string): string {
  return `/registry/providers/${[namespace, type].map(encodeURIComponent).join('/')}`;
}

/** The API path of one module. */
function modulePath(namespace: string, name: string, provider: string): string {
  return `/registry/modules/${[namespace, name, provider].map(encodeURIComponent).join('/')}`;
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
