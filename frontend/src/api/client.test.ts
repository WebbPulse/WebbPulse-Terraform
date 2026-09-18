import { describe, expect, it, vi } from 'vitest';

import {
  aConfigVersion,
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
      'GET /api/v1/workspaces': { body: { workspaces: [aWorkspace()] } },
    });
    const result = await api.listWorkspaces();
    expect(result.workspaces).toHaveLength(1);
    expect(result.workspaces[0]?.name).toBe('platform');
  });

  it('creates a workspace, posting the body', async () => {
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

  it('patches a workspace on its own path', async () => {
    const { api, transport } = apiOver({
      'PATCH /api/v1/workspaces/ws-1': { body: aWorkspace() },
    });
    await api.updateWorkspace('ws-1', { auto_apply: true });
    expect(transport.requests[0]?.method).toBe('PATCH');
    expect(transport.requests[0]?.path).toBe('/api/v1/workspaces/ws-1');
    expect(transport.requests[0]?.body).toEqual({ auto_apply: true });
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
  });
});

describe('TerraformApi variables', () => {
  it('lists variables and leaves a sensitive one without a value', async () => {
    const { api } = apiOver({
      'GET /api/v1/workspaces/ws-1/variables': {
        body: {
          variables: [
            aVariable(),
            aVariable({ key: 'token', sensitive: true, value: null }),
          ],
        },
      },
    });
    const result = await api.listVariables('ws-1');
    expect(result.variables[1]?.sensitive).toBe(true);
    expect(result.variables[1]?.value).toBeNull();
  });

  it('puts a variable on its keyed path', async () => {
    const { api, transport } = apiOver({
      'PUT /api/v1/workspaces/ws-1/variables/region': { body: aVariable() },
    });
    await api.putVariable('ws-1', 'region', {
      value: 'us-west-2',
      category: 'terraform',
      sensitive: false,
      hcl: false,
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

describe('TerraformApi config versions', () => {
  it('creates a configuration version and returns the presigned PUT', async () => {
    const { api } = apiOver({
      'POST /api/v1/workspaces/ws-1/config-versions': {
        status: 201,
        body: {
          config_version: aConfigVersion({ status: 'pending' }),
          upload_url: 'https://bucket.s3.test/configs/ws-1/cv-1.tar.gz?sig=1',
          upload_headers: { 'content-type': 'application/gzip' },
          expires_in: 900,
        },
      },
    });
    const result = await api.createConfigVersion('ws-1', { message: 'Seed.' });
    expect(result.upload_url).toContain('sig=1');
    expect(result.config_version.status).toBe('pending');
  });

  it('lists configuration versions', async () => {
    const { api } = apiOver({
      'GET /api/v1/workspaces/ws-1/config-versions': {
        body: { config_versions: [aConfigVersion()] },
      },
    });
    const result = await api.listConfigVersions('ws-1');
    expect(result.config_versions).toHaveLength(1);
  });
});

describe('TerraformApi runs', () => {
  it('lists runs scoped to a workspace through the query', async () => {
    const { api, transport } = apiOver({
      'GET /api/v1/runs': { body: { runs: [aRun()] } },
    });
    await api.listRuns({ workspace_id: 'ws-1' });
    expect(transport.requests[0]?.query.get('workspace_id')).toBe('ws-1');
  });

  it('omits the workspace filter when there is none', async () => {
    const { api, transport } = apiOver({
      'GET /api/v1/runs': { body: { runs: [] } },
    });
    await api.listRuns();
    expect(transport.requests[0]?.query.has('workspace_id')).toBe(false);
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
    expect((await api.confirmRun('run-1')).state).toBe('applying');
    expect((await api.cancelRun('run-1')).state).toBe('cancelled');
    expect(transport.requests[0]?.path).toBe('/api/v1/runs/run-1/confirm');
    expect(transport.requests[1]?.path).toBe('/api/v1/runs/run-1/cancel');
  });

  it('discards on its own route', async () => {
    const { api, transport } = apiOver({
      'POST /api/v1/runs/run-1/discard': { body: aRun('discarded') },
    });
    expect((await api.discardRun('run-1')).state).toBe('discarded');
    expect(transport.requests[0]?.path).toBe('/api/v1/runs/run-1/discard');
    expect(transport.requests[0]?.body).toEqual({});
  });

  it('sends the phase and the cursor on the logs route', async () => {
    const { api, transport } = apiOver({
      'GET /api/v1/runs/run-1/logs': {
        body: {
          run_id: 'run-1',
          phase: 'plan',
          lines: [{ timestamp: 1, message: 'Terraform will perform...' }],
          next_after: 'tok-2',
          complete: false,
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
          lines: [],
          next_after: null,
          complete: true,
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
});

describe('uploadConfigTarball', () => {
  it('PUTs the tarball to the presigned URL with its headers', async () => {
    const put = vi.fn(() =>
      Promise.resolve(new Response(null, { status: 200 }))
    );
    const upload = {
      config_version: aConfigVersion({ status: 'pending' }),
      upload_url: 'https://bucket.s3.test/configs/ws-1/cv-1.tar.gz?sig=1',
      upload_headers: { 'content-type': 'application/gzip' },
      expires_in: 900,
    };
    const file = new Blob(['tarball'], { type: 'application/gzip' });
    await uploadConfigTarball(upload, file, {
      fetch: put as unknown as typeof globalThis.fetch,
    });
    expect(put).toHaveBeenCalledWith(upload.upload_url, {
      method: 'PUT',
      body: file,
      headers: { 'content-type': 'application/gzip' },
    });
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
          upload_headers: null,
          expires_in: 900,
        },
        new Blob(['x']),
        { fetch: put as unknown as typeof globalThis.fetch }
      )
    ).rejects.toThrow('403');
  });
});
