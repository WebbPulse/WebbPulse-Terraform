import { act, fireEvent, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { aConfigVersion } from '../../test-helpers/fixtures';
import {
  renderWithAuth,
  signedInAuthClient,
} from '../../test-helpers/renderWithAuth';
import {
  apiClientModuleMock,
  apiMock,
  resetApiMock,
} from '../../test-helpers/apiMock';

vi.mock('../../api/client', () => apiClientModuleMock());

const { UploadConfigForm, UPLOAD_DEADLINE_MS } =
  await import('./UploadConfigForm');

const FORM = 'Upload a configuration version';

/** Mounts the form on its own. */
function renderForm(): void {
  renderWithAuth(
    <UploadConfigForm
      workspaceId="ws-01J000000000000000000000"
      queryKey="config-versions:ws-01J000000000000000000000"
      label={FORM}
      fileLabel="Configuration tarball"
    />,
    signedInAuthClient()
  );
}

/** Chooses a tarball in the file input. */
async function chooseFile(): Promise<void> {
  await userEvent.upload(
    screen.getByLabelText('Configuration tarball'),
    new File(['tarball'], 'config.tar.gz', { type: 'application/gzip' })
  );
}

/** A minted version whose PUT goes to a test bucket. */
function mintedUpload(): void {
  apiMock.createConfigVersion.mockResolvedValue({
    config_version: aConfigVersion({ status: 'pending' }),
    upload_url: 'https://bucket.s3.test/x',
    headers: {},
    expires_in: 900,
  });
}

describe('UploadConfigForm', () => {
  beforeEach(() => {
    resetApiMock();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('says so when submitted without a file', async () => {
    renderForm();

    fireEvent.submit(screen.getByRole('form', { name: FORM }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Choose a .tar.gz configuration archive first.'
    );
    expect(apiMock.createConfigVersion).not.toHaveBeenCalled();
  });

  it('mints only one version however often it is submitted', async () => {
    mintedUpload();
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response(null, { status: 200 })))
    );
    renderForm();
    await chooseFile();

    const form = screen.getByRole('form', { name: FORM });
    fireEvent.submit(form);
    fireEvent.submit(form);

    expect(
      await screen.findByText('Uploaded config.tar.gz.')
    ).toBeInTheDocument();
    expect(apiMock.createConfigVersion).toHaveBeenCalledTimes(1);
  });

  it('disables the file input and the button while uploading', async () => {
    apiMock.createConfigVersion.mockReturnValue(new Promise(() => undefined));
    renderForm();
    await chooseFile();

    fireEvent.submit(screen.getByRole('form', { name: FORM }));

    expect(
      await screen.findByLabelText('Configuration tarball')
    ).toBeDisabled();
    expect(
      screen.getByRole('button', { name: /Uploading the configuration/ })
    ).toBeDisabled();
  });

  it('shows a refused PUT', async () => {
    mintedUpload();
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response(null, { status: 403 })))
    );
    renderForm();
    await chooseFile();

    fireEvent.submit(screen.getByRole('form', { name: FORM }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Uploading the configuration tarball failed with 403.'
    );
  });

  it('shows a PUT that never reached storage', async () => {
    mintedUpload();
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('Failed to fetch')))
    );
    renderForm();
    await chooseFile();

    fireEvent.submit(screen.getByRole('form', { name: FORM }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'could not be sent to storage'
    );
  });

  it('gives up on an upload that hangs and says it timed out', async () => {
    mintedUpload();
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_url: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => {
              reject(new DOMException('Aborted', 'AbortError'));
            });
          })
      )
    );
    renderForm();
    await chooseFile();
    vi.useFakeTimers({ shouldAdvanceTime: true });

    fireEvent.submit(screen.getByRole('form', { name: FORM }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UPLOAD_DEADLINE_MS);
    });

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The upload timed out.'
    );
    expect(screen.getByLabelText('Configuration tarball')).toBeEnabled();
  });
});
