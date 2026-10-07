import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { TerraformApi, describeError } from '../api';
import {
  jsonResponse,
  renderWithAuth,
  stubAuthenticator,
} from '../test-helpers/renderWithAuth';
import {
  STEP_UP_FACTOR_KEY,
  StepUpBoundary,
  confirmationWindow,
  rememberedFactor,
  stepUpErrorMessage,
} from './StepUpBoundary';

/** The refusal the control plane answers a stale sign-in with. */
function stepUpRequired(): Response {
  return new Response(
    JSON.stringify({
      success: false,
      status: 401,
      message: 'Confirm your password to continue.',
      error_code: 'STEP_UP_REQUIRED',
      max_age: 900,
      request_id: 'r-test',
    }),
    {
      status: 401,
      headers: {
        'content-type': 'application/json',
        'www-authenticate':
          'Bearer error="insufficient_user_authentication", max_age=900',
      },
    }
  );
}

/** The authenticator code the fake backend accepts. */
const GOOD_CODE = '123456';

/** A fake backend: the key route refuses until a password, code or passkey is confirmed. */
function backend(correctPassword: string): {
  fetch: typeof globalThis.fetch;
  calls: string[];
} {
  const calls: string[] = [];
  let steppedUp = false;
  const fetch: typeof globalThis.fetch = (input, init) => {
    const url =
      typeof input === 'string'
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const path = new URL(url).pathname;
    calls.push(`${init?.method ?? 'GET'} ${path}`);
    if (path.endsWith('/auth/refresh')) {
      return Promise.resolve(
        jsonResponse({ access_token: 'login-token', expires_in: 3600 })
      );
    }
    if (path.endsWith('/auth/step-up/passkey/options')) {
      return Promise.resolve(
        jsonResponse({ challenge_id: 'ch-1', publicKey: {} })
      );
    }
    if (path.endsWith('/auth/step-up')) {
      const raw = typeof init?.body === 'string' ? init.body : '{}';
      const body = JSON.parse(raw) as {
        password?: string;
        code?: string;
        credential?: unknown;
      };
      const accepted =
        body.credential !== undefined ||
        (body.password !== undefined && body.password === correctPassword) ||
        (body.code !== undefined && body.code === GOOD_CODE);
      if (!accepted) {
        return Promise.resolve(
          jsonResponse(
            {
              success: false,
              status: 401,
              message: 'That password is not right.',
              error_code: 'INVALID_CREDENTIALS',
              request_id: 'r-test',
            },
            401
          )
        );
      }
      steppedUp = true;
      return Promise.resolve(
        jsonResponse({ access_token: 'stepped-token', expires_in: 600 })
      );
    }
    if (path.endsWith('/api-keys')) {
      return Promise.resolve(
        steppedUp
          ? jsonResponse({ key_id: 'k', key: 'wpk_x', name: 'agent' }, 201)
          : stepUpRequired()
      );
    }
    return Promise.resolve(jsonResponse({}, 404));
  };
  return { fetch, calls };
}

/** A button that mints a key and says how it went. */
function MintButton({ api }: { api: TerraformApi }): React.ReactElement {
  const [outcome, setOutcome] = useState('');
  return (
    <>
      <button
        type="button"
        onClick={() => {
          api.createApiKey({ name: 'agent' }).then(
            () => {
              setOutcome('minted');
            },
            (error: unknown) => {
              setOutcome(describeError(error));
            }
          );
        }}
      >
        Mint
      </button>
      <p data-testid="outcome">{outcome}</p>
    </>
  );
}

/** Mounts the boundary around the button, sharing the API's own auth client. */
function mount(
  password: string,
  webAuthn = stubAuthenticator()
): { calls: string[] } {
  const { fetch, calls } = backend(password);
  const api = new TerraformApi({
    baseUrl: 'https://api.test/api/v1',
    fetch,
    retries: 0,
    webAuthn,
  });
  renderWithAuth(
    <StepUpBoundary api={api}>
      <MintButton api={api} />
    </StepUpBoundary>,
    api.getAuthClient()
  );
  return { calls };
}

describe('confirmationWindow', () => {
  it('says the window in minutes', () => {
    expect(confirmationWindow(900)).toBe('15 minutes');
    expect(confirmationWindow(60)).toBe('1 minute');
    expect(confirmationWindow(null)).toBe('a few minutes');
  });
});

describe('stepUpErrorMessage', () => {
  it('says nothing for a dismissed passkey prompt', () => {
    expect(stepUpErrorMessage(null)).toBeNull();
    expect(
      stepUpErrorMessage({
        ok: false,
        reason: 'cancelled',
        code: undefined,
        message: 'Cancelled.',
      })
    ).toBeNull();
    expect(stepUpErrorMessage(new Error('That password is not right.'))).toBe(
      'That password is not right.'
    );
  });
});

beforeEach(() => {
  globalThis.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('rememberedFactor', () => {
  it('defaults to the authenticator and remembers a password', () => {
    expect(rememberedFactor()).toBe('code');
    globalThis.localStorage.setItem(STEP_UP_FACTOR_KEY, 'password');
    expect(rememberedFactor()).toBe('password');
    globalThis.localStorage.setItem(STEP_UP_FACTOR_KEY, 'junk');
    expect(rememberedFactor()).toBe('code');
  });
});

describe('StepUpBoundary', () => {
  it('confirms with a passkey and replays the call once', async () => {
    vi.stubGlobal('PublicKeyCredential', {});
    const user = userEvent.setup();
    const webAuthn = stubAuthenticator();
    const { calls } = mount('hunter22', webAuthn);

    await user.click(screen.getByRole('button', { name: 'Mint' }));
    await screen.findByRole('dialog', { name: 'Confirm it is you' });
    await user.click(screen.getByRole('button', { name: 'Use a passkey' }));

    await waitFor(() => {
      expect(screen.getByTestId('outcome')).toHaveTextContent('minted');
    });
    expect(webAuthn.get).toHaveBeenCalledTimes(1);
    expect(calls).toContain('POST /api/auth/step-up/passkey/options');
    expect(
      calls.filter((call) => call === 'POST /api/v1/api-keys')
    ).toHaveLength(2);
  });

  it('offers no passkey where the browser cannot run one', async () => {
    vi.stubGlobal('PublicKeyCredential', undefined);
    const user = userEvent.setup();
    mount('hunter22');

    await user.click(screen.getByRole('button', { name: 'Mint' }));
    await screen.findByRole('dialog', { name: 'Confirm it is you' });

    expect(
      screen.queryByRole('button', { name: 'Use a passkey' })
    ).not.toBeInTheDocument();
  });

  it('asks for the password on STEP_UP_REQUIRED and replays the call once', async () => {
    const user = userEvent.setup();
    const { calls } = mount('hunter22');

    await user.click(screen.getByRole('button', { name: 'Mint' }));
    const dialog = await screen.findByRole('dialog', {
      name: 'Confirm it is you',
    });
    expect(dialog).toHaveTextContent('15 minutes');

    await user.click(screen.getByRole('tab', { name: 'Password' }));
    await user.type(screen.getByLabelText('Password'), 'hunter22');
    await user.click(screen.getByRole('button', { name: 'Confirm password' }));

    await waitFor(() => {
      expect(screen.getByTestId('outcome')).toHaveTextContent('minted');
    });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(
      calls.filter((call) => call === 'POST /api/v1/api-keys')
    ).toHaveLength(2);
  });

  it('keeps the prompt open on a wrong password', async () => {
    const user = userEvent.setup();
    mount('hunter22');

    await user.click(screen.getByRole('button', { name: 'Mint' }));
    await user.click(await screen.findByRole('tab', { name: 'Password' }));
    await user.type(screen.getByLabelText('Password'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Confirm password' }));

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(
      screen.getByRole('dialog', { name: 'Confirm it is you' })
    ).toBeInTheDocument();
    expect(screen.getByTestId('outcome')).toHaveTextContent('');
  });

  it('tells the page nothing changed when the prompt is cancelled', async () => {
    const user = userEvent.setup();
    const { calls } = mount('hunter22');

    await user.click(screen.getByRole('button', { name: 'Mint' }));
    await screen.findByRole('dialog', { name: 'Confirm it is you' });
    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => {
      expect(screen.getByTestId('outcome')).toHaveTextContent(
        'You did not confirm it is you, so nothing changed.'
      );
    });
    expect(
      calls.filter((call) => call === 'POST /api/v1/api-keys')
    ).toHaveLength(1);
  });

  it('confirms with an authenticator code in place and replays the call once', async () => {
    const user = userEvent.setup();
    const { calls } = mount('hunter22');

    await user.click(screen.getByRole('button', { name: 'Mint' }));
    await screen.findByRole('dialog', { name: 'Confirm it is you' });
    expect(
      screen.getByRole('tab', { name: 'Authenticator code' })
    ).toHaveAttribute('aria-selected', 'true');

    await user.type(screen.getByLabelText('Authenticator code'), GOOD_CODE);
    await user.click(screen.getByRole('button', { name: 'Confirm code' }));

    await waitFor(() => {
      expect(screen.getByTestId('outcome')).toHaveTextContent('minted');
    });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(
      calls.filter((call) => call === 'POST /api/v1/api-keys')
    ).toHaveLength(2);
    expect(globalThis.localStorage.getItem(STEP_UP_FACTOR_KEY)).toBe('code');
  });

  it('keeps the prompt open on a wrong code', async () => {
    const user = userEvent.setup();
    mount('hunter22');

    await user.click(screen.getByRole('button', { name: 'Mint' }));
    await user.type(
      await screen.findByLabelText('Authenticator code'),
      '000000'
    );
    await user.click(screen.getByRole('button', { name: 'Confirm code' }));

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByLabelText('Authenticator code')).toHaveValue('');
    expect(screen.getByTestId('outcome')).toHaveTextContent('');
  });

  it('opens on the password for a browser that last confirmed with one', async () => {
    globalThis.localStorage.setItem(STEP_UP_FACTOR_KEY, 'password');
    const user = userEvent.setup();
    mount('hunter22');

    await user.click(screen.getByRole('button', { name: 'Mint' }));
    await screen.findByRole('dialog', { name: 'Confirm it is you' });
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
  });
});
