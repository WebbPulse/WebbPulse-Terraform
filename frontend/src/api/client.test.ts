import { describe, expect, it, vi } from 'vitest';

import {
  aConfigVersion,
  aDelivery,
  aNotification,
  aRun,
  aVariable,
  aWorkspace,
} from '../test-helpers/fixtures';
import { mockFetch } from '../test-helpers/mockFetch';
import { TerraformApi, describeError, uploadConfigTarball } from './client';

const BASE = 'https://api.staging.terraform.webbpulse.com/api/v1';

/** An API over a route table, with the recorded requests beside it. */
function apiOver(routes: Parameters<typeof mockFetch>[0]): {
  api: TerraformApi;
  transport: ReturnType<typeof mockFetch>;
} {
  const transport = mockFetch(routes);
  return {
    api: new TerraformApi({
      baseUrl: BASE,
      fetch: transport.fetch,
      retries: 0,
    }),
    transport,
  };
}

describe('TerraformApi workspaces', () => {
  it('lists workspaces', async () => {
    const { api } = apiOver({
      'GET /api/v1/workspaces': { body: { items: [aWorkspace()] } },
    });
    const result = await api.listWorkspaces();
    expect(result.items).toHaveLength(1);
    expect(result.items[0]?.name).toBe('platform');
  });

  it('creates a workspace without a run role, posting the body', async () => {
    const { api, transport } = apiOver({
      'POST /api/v1/workspaces': { status: 201, body: aWorkspace() },
    });
    await api.createWorkspace({
      name: 'platform',
      engine: 'tofu',
      engine_version: '1.11.0',
    });
    const request = transport.requests[0];
    expect(request?.method).toBe('POST');
    expect(request?.body).toEqual({
      name: 'platform',
      engine: 'tofu',
      engine_version: '1.11.0',
    });
  });

  it('reads the run role check with a GET that sends no body', async () => {
    const answer = {
      connected: false,
      status: 'unverified',
      account_id: null,
      error: 'No run has assumed this role yet.',
      run_id: null,
      checked_at: null,
    };
    const { api, transport } = apiOver({
      'GET /api/v1/workspaces/ws-1/run-role/check': { body: answer },
    });
    expect(await api.readRunRoleCheck('ws-1')).toEqual(answer);
    expect(transport.requests[0]?.method).toBe('GET');
    expect(transport.requests[0]?.path).toBe(
      '/api/v1/workspaces/ws-1/run-role/check'
    );
  });

  it('records the run role check on its own route with an empty POST', async () => {
    const answer = {
      connected: true,
      status: 'connected',
      account_id: '123456789012',
      error: null,
      run_id: 'run-1',
      checked_at: '2026-09-17T00:05:00Z',
    };
    const { api, transport } = apiOver({
      'POST /api/v1/workspaces/ws-1/run-role/check': { body: answer },
    });
    expect(await api.checkRunRole('ws-1')).toEqual(answer);
    expect(transport.requests[0]?.method).toBe('POST');
    expect(transport.requests[0]?.path).toBe(
      '/api/v1/workspaces/ws-1/run-role/check'
    );
    expect(transport.requests[0]?.body).toBeUndefined();
  });

  it('syncs the GitHub webhook with an empty POST', async () => {
    const answer = {
      url: 'https://api.staging.terraform.webbpulse.com/api/v1/github/webhooks',
      content_type: 'json',
      insecure_ssl: '0',
      events: ['push', 'pull_request'],
    };
    const { api, transport } = apiOver({
      'POST /api/v1/github/app/webhook': { body: answer },
    });
    expect(await api.syncGitHubWebhook()).toEqual(answer);
    expect(transport.requests[0]?.method).toBe('POST');
    expect(transport.requests[0]?.path).toBe('/api/v1/github/app/webhook');
    expect(transport.requests[0]?.body).toBeUndefined();
  });

  it('sends a confirm or discard comment as the body', async () => {
    const { api, transport } = apiOver({
      'POST /api/v1/runs/run-1/confirm': { body: aRun('applying') },
      'POST /api/v1/runs/run-1/discard': { body: aRun('discarded') },
    });
    await api.confirmRun('run-1', 'Reviewed.');
    await api.discardRun('run-1', '  ');
    expect(transport.requests[0]?.body).toEqual({ comment: 'Reviewed.' });
    expect(transport.requests[1]?.body).toBeUndefined();
  });

  it('starts quick setup with the account and policy in the body', async () => {
    const answer = {
      account_id: '123456789012',
      role_arn: 'arn:aws:iam::123456789012:role/control-plane-workspace-01J',
      role_name: 'control-plane-workspace-01J',
      stack_name: 'control-plane-workspace-01J',
      region: 'us-west-2',
      permissions_policy_arn: null,
      expires_in: 3600,
      console_url:
        'https://us-west-2.console.aws.amazon.com/cloudformation/home',
    };
    const { api, transport } = apiOver({
      'POST /api/v1/workspaces/ws-1/run-role/quick-setup': { body: answer },
    });
    expect(
      await api.startRunRoleQuickSetup('ws-1', {
        account_id: '123456789012',
        permissions: 'none',
      })
    ).toEqual(answer);
    expect(transport.requests[0]?.body).toEqual({
      account_id: '123456789012',
      permissions: 'none',
    });
  });

  it('patches a workspace on its own path', async () => {
    const { api, transport } = apiOver({
      'PATCH /api/v1/workspaces/ws-1': { body: aWorkspace() },
    });
    await api.updateWorkspace('ws-1', {
      run_role_arn: 'arn:aws:iam::123456789012:role/other-run',
    });
    expect(transport.requests[0]?.method).toBe('PATCH');
    expect(transport.requests[0]?.path).toBe('/api/v1/workspaces/ws-1');
    expect(transport.requests[0]?.body).toEqual({
      run_role_arn: 'arn:aws:iam::123456789012:role/other-run',
    });
  });

  it('escapes an id into the path', async () => {
    const { api, transport } = apiOver({
      'GET /api/v1/workspaces/ws%2F1': { body: aWorkspace() },
    });
    await api.getWorkspace('ws/1');
    expect(transport.requests[0]?.path).toBe('/api/v1/workspaces/ws%2F1');
  });

  it('deletes a workspace', async () => {
    const { api, transport } = apiOver({
      'DELETE /api/v1/workspaces/ws-1': { status: 204 },
    });
    await api.deleteWorkspace('ws-1');
    expect(transport.requests[0]?.method).toBe('DELETE');
    expect(transport.requests[0]?.query.has('force')).toBe(false);
  });

  it('asks for a force delete through the query', async () => {
    const { api, transport } = apiOver({
      'DELETE /api/v1/workspaces/ws-1': { status: 204 },
    });
    await api.deleteWorkspace('ws-1', { force: true });
    expect(transport.requests[0]?.query.get('force')).toBe('true');
  });
});

describe('TerraformApi variables', () => {
  it('lists variables and leaves a sensitive one without a value', async () => {
    const { api } = apiOver({
      'GET /api/v1/workspaces/ws-1/variables': {
        body: {
          items: [
            aVariable(),
            aVariable({ key: 'token', sensitive: true, value: null }),
          ],
        },
      },
    });
    const result = await api.listVariables('ws-1');
    expect(result.items[1]?.sensitive).toBe(true);
    expect(result.items[1]?.value).toBeNull();
  });

  it('puts a variable on its keyed path', async () => {
    const { api, transport } = apiOver({
      'PUT /api/v1/workspaces/ws-1/variables/region': { body: aVariable() },
    });
    await api.putVariable('ws-1', 'region', {
      value: 'us-west-2',
      category: 'terraform',
      sensitive: false,
    });
    expect(transport.requests[0]?.method).toBe('PUT');
    expect(transport.requests[0]?.path).toBe(
      '/api/v1/workspaces/ws-1/variables/region'
    );
  });

  it('deletes a variable', async () => {
    const { api, transport } = apiOver({
      'DELETE /api/v1/workspaces/ws-1/variables/region': { status: 204 },
    });
    await api.deleteVariable('ws-1', 'region');
    expect(transport.requests[0]?.method).toBe('DELETE');
  });
});

describe('TerraformApi notification configurations', () => {
  const PATH = '/api/v1/workspaces/ws-1/notification-configurations';

  it('lists and creates configurations on the workspace', async () => {
    const { api, transport } = apiOver({
      [`GET ${PATH}`]: { body: { items: [aNotification()] } },
      [`POST ${PATH}`]: { status: 201, body: aNotification() },
    });
    const list = await api.listNotificationConfigurations('ws-1');
    expect(list.items).toHaveLength(1);
    await api.createNotificationConfiguration('ws-1', {
      name: 'Team channel',
      destination_type: 'slack',
      url: 'https://hooks.slack.com/services/T/B/x',
    });
    expect(transport.requests[1]?.method).toBe('POST');
    expect(transport.requests[1]?.body).toEqual({
      name: 'Team channel',
      destination_type: 'slack',
      url: 'https://hooks.slack.com/services/T/B/x',
    });
  });

  it('edits, deletes and test sends one configuration', async () => {
    const { api, transport } = apiOver({
      [`PATCH ${PATH}/nc-1`]: { body: aNotification({ enabled: false }) },
      [`DELETE ${PATH}/nc-1`]: { status: 204 },
      [`POST ${PATH}/nc-1/actions/verify`]: { body: aDelivery() },
    });
    await api.updateNotificationConfiguration('ws-1', 'nc-1', {
      enabled: false,
    });
    await api.deleteNotificationConfiguration('ws-1', 'nc-1');
    const delivery = await api.verifyNotificationConfiguration('ws-1', 'nc-1');
    expect(transport.requests.map((request) => request.method)).toEqual([
      'PATCH',
      'DELETE',
      'POST',
    ]);
    expect(delivery.status).toBe('succeeded');
  });
});

describe('TerraformApi config versions', () => {
  it('creates a configuration version and returns the presigned PUT', async () => {
    const { api, transport } = apiOver({
      'POST /api/v1/workspaces/ws-1/config-versions': {
        status: 201,
        body: {
          config_version: aConfigVersion({ status: 'pending' }),
          upload_url: 'https://bucket.s3.test/configs/ws-1/cv-1.tar.gz?sig=1',
          headers: {
            'Content-Type': 'application/gzip',
            'Content-Length': '2048',
          },
          expires_in: 900,
        },
      },
    });
    const result = await api.createConfigVersion('ws-1', { size_bytes: 2048 });
    expect(transport.requests[0]?.body).toEqual({ size_bytes: 2048 });
    expect(result.upload_url).toContain('sig=1');
    expect(result.headers).toEqual({
      'Content-Type': 'application/gzip',
      'Content-Length': '2048',
    });
    expect(result.config_version.status).toBe('pending');
  });

  it('lists configuration versions', async () => {
    const { api } = apiOver({
      'GET /api/v1/workspaces/ws-1/config-versions': {
        body: { items: [aConfigVersion()] },
      },
    });
    const result = await api.listConfigVersions('ws-1');
    expect(result.items).toHaveLength(1);
  });
});

describe('TerraformApi runs', () => {
  it('lists runs scoped to a workspace through the query', async () => {
    const { api, transport } = apiOver({
      'GET /api/v1/runs': { body: { items: [aRun()] } },
    });
    await api.listRuns({ workspace_id: 'ws-1' });
    expect(transport.requests[0]?.query.get('workspace_id')).toBe('ws-1');
  });

  it('lists across workspaces with only the paging it was given', async () => {
    const { api, transport } = apiOver({
      'GET /api/v1/runs': { body: { items: [], next_cursor: null } },
    });
    await api.listRuns({ limit: 10, cursor: 'run-1' });
    const query = transport.requests[0]?.query;
    expect(query?.has('workspace_id')).toBe(false);
    expect(query?.get('limit')).toBe('10');
    expect(query?.get('cursor')).toBe('run-1');
  });

  it('sends no query at all for the first cross-workspace page', async () => {
    const { api, transport } = apiOver({
      'GET /api/v1/runs': { body: { items: [], next_cursor: null } },
    });
    await api.listRuns();
    expect([...(transport.requests[0]?.query.keys() ?? [])]).toEqual([]);
  });

  it('starts a run with the contract body', async () => {
    const { api, transport } = apiOver({
      'POST /api/v1/runs': { status: 201, body: aRun('pending') },
    });
    await api.createRun({
      workspace_id: 'ws-1',
      config_version_id: 'cv-1',
      plan_only: true,
      message: 'Seed.',
    });
    expect(transport.requests[0]?.body).toEqual({
      workspace_id: 'ws-1',
      config_version_id: 'cv-1',
      plan_only: true,
      message: 'Seed.',
    });
  });

  it('confirms and cancels on their own routes', async () => {
    const { api, transport } = apiOver({
      'POST /api/v1/runs/run-1/confirm': { body: aRun('applying') },
      'POST /api/v1/runs/run-1/cancel': { body: aRun('cancelled') },
    });
    expect((await api.confirmRun('run-1')).status).toBe('applying');
    expect((await api.cancelRun('run-1')).status).toBe('cancelled');
    expect(transport.requests[0]?.path).toBe('/api/v1/runs/run-1/confirm');
    expect(transport.requests[1]?.path).toBe('/api/v1/runs/run-1/cancel');
  });

  it('confirms without going through the step-up prompt', async () => {
    const { api } = apiOver({
      'POST /api/v1/runs/run-1/confirm': { body: aRun('applying') },
    });
    const gate = vi.fn(() => () => Promise.reject(new Error('prompted')));
    api.setStepUpGate(gate);
    expect((await api.confirmRun('run-1')).status).toBe('applying');
    expect(gate).not.toHaveBeenCalled();
  });

  it('discards on its own route', async () => {
    const { api, transport } = apiOver({
      'POST /api/v1/runs/run-1/discard': { body: aRun('discarded') },
    });
    expect((await api.discardRun('run-1')).status).toBe('discarded');
    expect(transport.requests[0]?.path).toBe('/api/v1/runs/run-1/discard');
    expect(transport.requests[0]?.body).toBeUndefined();
  });

  it('sends the phase and the cursor on the logs route', async () => {
    const { api, transport } = apiOver({
      'GET /api/v1/runs/run-1/logs': {
        body: {
          run_id: 'run-1',
          phase: 'plan',
          events: [{ timestamp: 1, message: 'Terraform will perform...' }],
          next_after: 'tok-2',
        },
      },
    });
    const page = await api.getRunLogs('run-1', {
      phase: 'plan',
      after: 'tok-1',
    });
    expect(page.next_after).toBe('tok-2');
    expect(transport.requests[0]?.query.get('phase')).toBe('plan');
    expect(transport.requests[0]?.query.get('after')).toBe('tok-1');
  });

  it('omits the cursor on the first page', async () => {
    const { api, transport } = apiOver({
      'GET /api/v1/runs/run-1/logs': {
        body: {
          run_id: 'run-1',
          phase: 'plan',
          events: [],
          next_after: null,
        },
      },
    });
    await api.getRunLogs('run-1', { phase: 'plan', after: null });
    expect(transport.requests[0]?.query.has('after')).toBe(false);
  });
});

describe('TerraformApi failures', () => {
  it('rejects on a non 2xx and describes the envelope message', async () => {
    const { api } = apiOver({
      'GET /api/v1/workspaces': {
        status: 403,
        body: {
          success: false,
          status: 403,
          message: 'Missing the workspaces:read scope.',
          request_id: 'r-1',
        },
      },
    });
    await expect(api.listWorkspaces()).rejects.toThrow();
    const error = await api.listWorkspaces().catch((thrown: unknown) => thrown);
    expect(describeError(error)).toBe('Missing the workspaces:read scope.');
  });

  it('describes a plain error and an unknown throw', () => {
    expect(describeError(new Error('Boom.'))).toBe('Boom.');
    expect(describeError('nope')).toBe('The request failed. Please try again.');
  });

  it('describes a refused sign-in from the identity envelope', async () => {
    const { api } = apiOver({
      'GET /api/v1/workspaces': {
        status: 401,
        body: {
          success: false,
          status: 401,
          message: 'Wrong email or password.',
          error_code: 'INVALID_CREDENTIALS',
          request_id: 'r-2',
        },
      },
    });
    const error = await api.listWorkspaces().catch((thrown: unknown) => thrown);
    expect(describeError(error)).toBe('Wrong email or password.');
  });

  it('falls back rather than rendering an error with no message', () => {
    expect(describeError(new Error(''))).toBe(
      'The request failed. Please try again.'
    );
  });
});

describe('uploadConfigTarball', () => {
  it('PUTs the tarball to the presigned URL with its headers', async () => {
    const put = vi.fn(() =>
      Promise.resolve(new Response(null, { status: 200 }))
    );
    const upload = {
      config_version: aConfigVersion({ status: 'pending' }),
      upload_url: 'https://bucket.s3.test/configs/ws-1/cv-1.tar.gz?sig=1',
      headers: {
        'Content-Type': 'application/gzip',
        'Content-Length': '7',
      },
      expires_in: 900,
    };
    const file = new Blob(['tarball'], { type: 'application/gzip' });
    await uploadConfigTarball(upload, file, {
      fetch: put as unknown as typeof globalThis.fetch,
    });
    expect(put).toHaveBeenCalledWith(upload.upload_url, {
      method: 'PUT',
      body: file,
      headers: {
        'Content-Type': 'application/gzip',
        'Content-Length': '7',
      },
    });
  });

  it('sends every signed header, not only the content type', async () => {
    const put = vi.fn<typeof globalThis.fetch>(() =>
      Promise.resolve(new Response(null, { status: 200 }))
    );
    const headers = {
      'Content-Type': 'application/gzip',
      'Content-Length': '7',
      'x-amz-server-side-encryption': 'aws:kms',
    };
    await uploadConfigTarball(
      {
        config_version: aConfigVersion({ status: 'pending' }),
        upload_url: 'https://bucket.s3.test/x',
        headers,
        expires_in: 900,
      },
      new Blob(['tarball'], { type: 'application/gzip' }),
      { fetch: put }
    );
    expect(put.mock.calls[0]?.[1]?.headers).toEqual(headers);
  });

  it('throws when S3 refuses the upload', async () => {
    const put = vi.fn(() =>
      Promise.resolve(new Response(null, { status: 403 }))
    );
    await expect(
      uploadConfigTarball(
        {
          config_version: aConfigVersion(),
          upload_url: 'https://bucket.s3.test/x',
          headers: { 'Content-Type': 'application/gzip' },
          expires_in: 900,
        },
        new Blob(['x']),
        { fetch: put as unknown as typeof globalThis.fetch }
      )
    ).rejects.toThrow('403');
  });
});
